// dpnetshim - COM interception shim for DirectPlay8 (dpnet.dll).
//
// Two Worlds does not link dpnet.dll; it creates DirectPlay8 objects via
// CoCreateInstance. Registering this DLL for those CLSIDs under HKCU
// therefore puts us in front of the real implementation without touching
// any system file and without administrator rights.
//
// Stage 1 (this file): stay fully transparent - forward every class
// request to the real dpnet.dll and log what the game asks for, plus the
// vtable calls it makes on the objects it gets back. The log tells us
// exactly which DirectPlay call crashes, and proves the interception
// path works before any behaviour is reimplemented.
//
// Build: see build.bat (32-bit, matches the game).

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <unknwn.h>
#include <stdio.h>

static HMODULE g_realDpnet = NULL;
static LONG g_objects = 0;
static CRITICAL_SECTION g_logLock;
static bool g_logReady = false;

// ---------------------------------------------------------------- logging

static void LogInit()
{
    if (!g_logReady) {
        InitializeCriticalSection(&g_logLock);
        g_logReady = true;
    }
}

static void Log(const char* fmt, ...)
{
    if (!g_logReady) return;
    EnterCriticalSection(&g_logLock);
    FILE* f = NULL;
    if (fopen_s(&f, "C:\\Users\\marco\\Desktop\\twMP\\dpnetshim.log", "a") == 0 && f) {
        SYSTEMTIME st;
        GetLocalTime(&st);
        fprintf(f, "[%02d:%02d:%02d.%03d] ", st.wHour, st.wMinute,
                st.wSecond, st.wMilliseconds);
        va_list args;
        va_start(args, fmt);
        vfprintf(f, fmt, args);
        va_end(args);
        fprintf(f, "\n");
        fclose(f);
    }
    LeaveCriticalSection(&g_logLock);
}

static void GuidToStr(REFGUID g, char* out, size_t n)
{
    sprintf_s(out, n,
              "{%08lX-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X}",
              g.Data1, g.Data2, g.Data3, g.Data4[0], g.Data4[1], g.Data4[2],
              g.Data4[3], g.Data4[4], g.Data4[5], g.Data4[6], g.Data4[7]);
}

// Known DirectPlay8 identifiers, for readable logs.
static const char* NameForGuid(REFGUID g)
{
    struct Entry { const char* name; GUID guid; };
    static const Entry kn[] = {
        { "CLSID_DirectPlay8Peer",    { 0x286F484D, 0x375E, 0x4458, { 0xA2, 0x72, 0xB1, 0x38, 0xE2, 0xF8, 0x0A, 0x6A } } },
        { "CLSID_DirectPlay8Client",  { 0x743F1DC6, 0x5ABA, 0x429F, { 0x8B, 0xDF, 0xC5, 0x4D, 0x03, 0x25, 0x3D, 0xC2 } } },
        { "CLSID_DirectPlay8Server",  { 0xDA825E1B, 0x6830, 0x43D7, { 0x83, 0x5D, 0x0B, 0x5A, 0xD8, 0x29, 0x56, 0xA2 } } },
        { "CLSID_DirectPlay8Address", { 0x934A9523, 0xA3CA, 0x4BC5, { 0xAD, 0xA0, 0xD6, 0xD9, 0x5D, 0x97, 0x94, 0x21 } } },
        { "CLSID_DP8SP_TCPIP",        { 0xEBFE7BA0, 0x628D, 0x11D2, { 0xAE, 0x0F, 0x00, 0x60, 0x97, 0xB0, 0x14, 0x11 } } },
        { "IID_IUnknown",             { 0x00000000, 0x0000, 0x0000, { 0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } } },
        { "IID_IClassFactory",        { 0x00000001, 0x0000, 0x0000, { 0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } } },
        { "IID_IDirectPlay8Peer",     { 0x5023C6, 0x000, 0x000, { 0, 0, 0, 0, 0, 0, 0, 0 } } },
    };
    for (size_t i = 0; i < sizeof(kn) / sizeof(kn[0]); ++i)
        if (IsEqualGUID(kn[i].guid, g)) return kn[i].name;
    return NULL;
}

static void LogGuid(const char* what, REFGUID g)
{
    char buf[64];
    GuidToStr(g, buf, sizeof(buf));
    const char* name = NameForGuid(g);
    if (name) Log("  %s = %s %s", what, buf, name);
    else      Log("  %s = %s", what, buf);
}

// ------------------------------------------------------- vtable call proxy
//
// A generic proxy: it copies the real object's vtable pointer and puts a
// thunk in front of every slot, so each call is logged with its slot
// index before being forwarded unchanged. Slot indices map to the
// documented method order of the interface, which is enough to identify
// the call that crashes without needing the SDK headers.

#define MAX_SLOTS 64

struct Proxy;
static Proxy* g_proxies[64];
static LONG g_proxyCount = 0;

struct Proxy {
    void**  vtbl;              // our vtable, must be first
    IUnknown* real;            // the wrapped object
    char    label[64];
    void*   thunks[MAX_SLOTS];
    void*   slots[MAX_SLOTS + 1];
};

// Writing 64 thunks by hand is unwieldy, so they are generated at
// runtime: each stub pushes its slot info and jumps to a common handler.
#pragma pack(push, 1)
struct Stub {
    BYTE  push_imm;     // 68 xx xx xx xx : push slotinfo
    DWORD imm;
    BYTE  jmp;          // E9 rel32       : jmp common
    DWORD rel;
};
#pragma pack(pop)

struct SlotInfo { Proxy* proxy; DWORD slot; };
static SlotInfo g_slotInfos[64][MAX_SLOTS];

// Common handler: logs, then transfers to the real function. The pushed
// SlotInfo* sits where the return address would be, so we fix the stack
// and jump so the real callee sees the original arguments untouched.
static void __stdcall LogCall(SlotInfo* si)
{
    Log("CALL %s slot %lu", si->proxy->label, si->slot);
}

extern "C" __declspec(naked) void CommonThunk()
{
    __asm {
        // stack: [SlotInfo*][retaddr][args...]
        pushad
        pushfd
        mov  eax, [esp + 36]        // SlotInfo*
        push eax
        call LogCall
        popfd
        popad
        // replace pushed SlotInfo* with the real function pointer
        push ebx
        mov  ebx, [esp + 4]         // SlotInfo*
        mov  eax, [ebx]             // proxy
        mov  edx, [ebx + 4]         // slot
        mov  eax, [eax + 4]         // proxy->real
        mov  eax, [eax]             // real vtable
        mov  eax, [eax + edx * 4]   // real function
        mov  [esp + 4], eax         // overwrite SlotInfo* with target
        pop  ebx
        ret                         // 'return' into the real function
    }
}

static IUnknown* WrapObject(IUnknown* real, const char* label)
{
    LONG idx = InterlockedIncrement(&g_proxyCount) - 1;
    if (idx >= 64) return real;   // out of proxy slots; stay transparent

    Proxy* p = (Proxy*)VirtualAlloc(NULL, sizeof(Proxy) + sizeof(Stub) * MAX_SLOTS,
                                    MEM_COMMIT | MEM_RESERVE,
                                    PAGE_EXECUTE_READWRITE);
    if (!p) return real;
    memset(p, 0, sizeof(Proxy));
    p->real = real;
    strcpy_s(p->label, sizeof(p->label), label);
    g_proxies[idx] = p;

    Stub* stubs = (Stub*)((BYTE*)p + sizeof(Proxy));
    for (DWORD s = 0; s < MAX_SLOTS; ++s) {
        g_slotInfos[idx][s].proxy = p;
        g_slotInfos[idx][s].slot = s;
        stubs[s].push_imm = 0x68;
        stubs[s].imm = (DWORD)&g_slotInfos[idx][s];
        stubs[s].jmp = 0xE9;
        stubs[s].rel = (DWORD)((BYTE*)CommonThunk - ((BYTE*)&stubs[s] + 10));
        p->slots[s] = &stubs[s];
    }
    p->vtbl = p->slots;
    Log("WRAP %s -> proxy %ld", label, idx);
    return (IUnknown*)p;
}

// ------------------------------------------------------- class factory

typedef HRESULT (WINAPI *PFN_DllGetClassObject)(REFCLSID, REFIID, LPVOID*);

class ShimFactory : public IClassFactory {
public:
    ShimFactory(IClassFactory* real, REFCLSID clsid)
        : m_ref(1), m_real(real), m_clsid(clsid) {}

    STDMETHOD(QueryInterface)(REFIID riid, void** ppv) {
        if (IsEqualIID(riid, IID_IUnknown) || IsEqualIID(riid, IID_IClassFactory)) {
            *ppv = this; AddRef(); return S_OK;
        }
        return m_real->QueryInterface(riid, ppv);
    }
    STDMETHOD_(ULONG, AddRef)()  { return InterlockedIncrement(&m_ref); }
    STDMETHOD_(ULONG, Release)() {
        LONG r = InterlockedDecrement(&m_ref);
        if (r == 0) { m_real->Release(); delete this; }
        return r;
    }
    STDMETHOD(CreateInstance)(IUnknown* outer, REFIID riid, void** ppv) {
        char cls[64]; GuidToStr(m_clsid, cls, sizeof(cls));
        const char* nm = NameForGuid(m_clsid);
        Log("CreateInstance %s", nm ? nm : cls);
        LogGuid("riid", riid);
        HRESULT hr = m_real->CreateInstance(outer, riid, ppv);
        Log("  -> hr=0x%08lX obj=%p", hr, ppv ? *ppv : NULL);
        if (SUCCEEDED(hr) && ppv && *ppv) {
            char label[64];
            sprintf_s(label, sizeof(label), "%s", nm ? nm : cls);
            *ppv = WrapObject((IUnknown*)*ppv, label);
        }
        return hr;
    }
    STDMETHOD(LockServer)(BOOL lock) { return m_real->LockServer(lock); }

private:
    LONG m_ref;
    IClassFactory* m_real;
    CLSID m_clsid;
};

// ------------------------------------------------------------- exports

STDAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, LPVOID* ppv)
{
    LogInit();
    char cls[64]; GuidToStr(rclsid, cls, sizeof(cls));
    const char* nm = NameForGuid(rclsid);
    Log("DllGetClassObject %s", nm ? nm : cls);
    LogGuid("clsid", rclsid);
    LogGuid("riid", riid);

    if (!g_realDpnet) {
        char path[MAX_PATH];
        GetSystemDirectoryA(path, MAX_PATH);   // SysWOW64 for a 32-bit process
        strcat_s(path, MAX_PATH, "\\dpnet.dll");
        g_realDpnet = LoadLibraryA(path);
        Log("  loaded real dpnet: %s -> %p", path, g_realDpnet);
    }
    if (!g_realDpnet) return CLASS_E_CLASSNOTAVAILABLE;

    PFN_DllGetClassObject real = (PFN_DllGetClassObject)
        GetProcAddress(g_realDpnet, "DllGetClassObject");
    if (!real) { Log("  ! real DllGetClassObject missing"); return CLASS_E_CLASSNOTAVAILABLE; }

    HRESULT hr = real(rclsid, riid, ppv);
    Log("  real -> hr=0x%08lX", hr);
    if (SUCCEEDED(hr) && IsEqualIID(riid, IID_IClassFactory) && ppv && *ppv) {
        *ppv = new ShimFactory((IClassFactory*)*ppv, rclsid);
        InterlockedIncrement(&g_objects);
    }
    return hr;
}

STDAPI DllCanUnloadNow(void)
{
    return g_objects == 0 ? S_OK : S_FALSE;
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(inst);
        LogInit();
        Log("=== dpnetshim geladen (pid %lu) ===", GetCurrentProcessId());
    }
    return TRUE;
}
