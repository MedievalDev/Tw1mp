// dpnetreplace - a minimal DirectPlay8 replacement for Two Worlds 1.
//
// Implements the DirectPlay8 COM objects the game creates (Peer + Address)
// over its own logic instead of the Windows dpnet.dll, so multiplayer no
// longer depends on the Windows DirectPlay legacy feature. Registered
// per-user under WOW6432Node (admin-free), it is loaded by the 32-bit game
// via CoCreateInstance.
//
// Phase 1 scope (verified against a captured real session): the solo-host
// path. The game hosts a session, gets its local player, and exchanges
// gameplay messages. In a solo session those messages loop back to the
// local player, so this build delivers every SendTo back as a RECEIVE and
// completes it - no real socket yet. That is enough for a single player to
// start and play a map through our DLL. Real UDP transport and a second
// peer (Connect/EnumHosts) come in a later phase.
//
// Only the 10 methods the game actually calls are implemented; the rest
// report DPNERR_UNSUPPORTED. Messages delivered: CREATE_PLAYER,
// DESTROY_PLAYER, RECEIVE, SEND_COMPLETE.

#define WIN32_LEAN_AND_MEAN
#define INITGUID
#include <windows.h>
#include <dplay8.h>
#include <dpaddr.h>
#include <stdio.h>
#include <vector>
#include <map>
#include <deque>
#include <string>

static CRITICAL_SECTION g_logLock;
static bool g_logReady = false;
static LONG g_objects = 0;

#define LOGPATH "C:\\Users\\marco\\Desktop\\twMP\\dpnetreplace.log"

static void LogInit() {
    if (!g_logReady) { InitializeCriticalSection(&g_logLock); g_logReady = true; }
}
static void Log(const char* fmt, ...) {
    if (!g_logReady) return;
    EnterCriticalSection(&g_logLock);
    FILE* f = NULL;
    if (fopen_s(&f, LOGPATH, "a") == 0 && f) {
        SYSTEMTIME st; GetLocalTime(&st);
        fprintf(f, "[%02d:%02d:%02d.%03d] ", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
        va_list a; va_start(a, fmt); vfprintf(f, fmt, a); va_end(a);
        fprintf(f, "\n"); fclose(f);
    }
    LeaveCriticalSection(&g_logLock);
}

static const DPNID LOCAL_PLAYER = 1;

// ===================================================================
//  IDirectPlay8Address
// ===================================================================

class ReplAddress : public IDirectPlay8Address {
public:
    ReplAddress() : m_ref(1) { InterlockedIncrement(&g_objects); }

    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (IsEqualIID(riid, IID_IUnknown) || IsEqualIID(riid, IID_IDirectPlay8Address)) {
            *ppv = static_cast<IDirectPlay8Address*>(this); AddRef(); return S_OK;
        }
        *ppv = NULL; return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&m_ref); }
    STDMETHODIMP_(ULONG) Release() override {
        LONG r = InterlockedDecrement(&m_ref);
        if (r == 0) { InterlockedDecrement(&g_objects); delete this; }
        return r;
    }

    STDMETHODIMP SetSP(const GUID* pguidSP) override {
        if (pguidSP) { m_sp = *pguidSP; m_hasSP = true; }
        Log("Addr::SetSP");
        return S_OK;
    }
    STDMETHODIMP GetSP(GUID* pguidSP) override {
        if (!m_hasSP) return DPNERR_DOESNOTEXIST;
        if (pguidSP) *pguidSP = m_sp;
        return S_OK;
    }
    STDMETHODIMP SetDevice(const GUID* pDevGuid) override {
        if (pDevGuid) { m_device = *pDevGuid; m_hasDevice = true; }
        return S_OK;
    }
    STDMETHODIMP GetDevice(GUID* pDevGuid) override {
        if (!m_hasDevice) return DPNERR_DOESNOTEXIST;
        if (pDevGuid) *pDevGuid = m_device;
        return S_OK;
    }

    // Components are stored as name -> (type, value). We keep string and
    // dword components (the ones the TCP/IP provider uses: hostname, port).
    STDMETHODIMP AddComponent(const WCHAR* pwszName, const void* lpvData,
                              DWORD dwDataSize, DWORD dwDataType) override {
        if (!pwszName || !lpvData) return DPNERR_INVALIDPARAM;
        Comp c; c.name = pwszName; c.type = dwDataType;
        if (dwDataType == DPNA_DATATYPE_DWORD) {
            c.dword = *reinterpret_cast<const DWORD*>(lpvData);
        } else { // string (wide) - store as-is
            c.str.assign(reinterpret_cast<const WCHAR*>(lpvData),
                         dwDataSize / sizeof(WCHAR));
            while (!c.str.empty() && c.str.back() == L'\0') c.str.pop_back();
        }
        // replace existing component of the same name
        for (auto& e : m_comps) if (e.name == c.name) { e = c; Log("Addr::AddComponent (replace)"); return S_OK; }
        m_comps.push_back(c);
        Log("Addr::AddComponent");
        return S_OK;
    }
    STDMETHODIMP GetNumComponents(DWORD* pdwNumComponents) override {
        if (pdwNumComponents) *pdwNumComponents = (DWORD)m_comps.size();
        return S_OK;
    }
    STDMETHODIMP GetComponentByName(const WCHAR* pwszName, void* pvBuffer,
                                    DWORD* pdwBufferSize, DWORD* pdwDataType) override {
        for (auto& c : m_comps)
            if (c.name == pwszName) return CopyComponent(c, pvBuffer, pdwBufferSize, pdwDataType);
        return DPNERR_DOESNOTEXIST;
    }
    STDMETHODIMP GetComponentByIndex(DWORD dwIndex, WCHAR* pwszName, DWORD* pdwNameLen,
                                     void* pvBuffer, DWORD* pdwBufferSize,
                                     DWORD* pdwDataType) override {
        if (dwIndex >= m_comps.size()) return DPNERR_DOESNOTEXIST;
        Comp& c = m_comps[dwIndex];
        DWORD needName = (DWORD)c.name.size() + 1;
        if (!pwszName || !pdwNameLen || *pdwNameLen < needName) {
            if (pdwNameLen) *pdwNameLen = needName;
            return DPNERR_BUFFERTOOSMALL;
        }
        wcscpy_s(pwszName, *pdwNameLen, c.name.c_str());
        *pdwNameLen = needName;
        return CopyComponent(c, pvBuffer, pdwBufferSize, pdwDataType);
    }

    STDMETHODIMP GetURLW(WCHAR* pwszURL, PDWORD pdwNumChars) override {
        std::wstring url = BuildURL();
        DWORD need = (DWORD)url.size() + 1;
        if (!pwszURL || !pdwNumChars || *pdwNumChars < need) {
            if (pdwNumChars) *pdwNumChars = need;
            return DPNERR_BUFFERTOOSMALL;
        }
        wcscpy_s(pwszURL, *pdwNumChars, url.c_str());
        *pdwNumChars = need;
        Log("Addr::GetURLW -> %S", url.c_str());
        return S_OK;
    }
    STDMETHODIMP GetURLA(CHAR* pszURL, PDWORD pdwNumChars) override {
        std::wstring w = BuildURL();
        std::string a(w.begin(), w.end());
        DWORD need = (DWORD)a.size() + 1;
        if (!pszURL || !pdwNumChars || *pdwNumChars < need) {
            if (pdwNumChars) *pdwNumChars = need; return DPNERR_BUFFERTOOSMALL;
        }
        strcpy_s(pszURL, *pdwNumChars, a.c_str());
        *pdwNumChars = need;
        return S_OK;
    }

    STDMETHODIMP BuildFromURLW(WCHAR* pwszURL) override {
        Log("Addr::BuildFromURLW <- %S", pwszURL ? pwszURL : L"(null)");
        return ParseURL(pwszURL ? std::wstring(pwszURL) : std::wstring());
    }
    STDMETHODIMP BuildFromURLA(CHAR* pszURL) override {
        std::wstring w;
        if (pszURL) { std::string s(pszURL); w.assign(s.begin(), s.end()); }
        return ParseURL(w);
    }

    STDMETHODIMP Duplicate(IDirectPlay8Address** ppdpaNewAddress) override {
        if (!ppdpaNewAddress) return DPNERR_INVALIDPARAM;
        ReplAddress* a = new ReplAddress();
        a->m_sp = m_sp; a->m_hasSP = m_hasSP;
        a->m_device = m_device; a->m_hasDevice = m_hasDevice;
        a->m_comps = m_comps;
        *ppdpaNewAddress = a;
        return S_OK;
    }
    STDMETHODIMP SetEqual(IDirectPlay8Address* pdpaAddress) override {
        // best effort: copy via URL round-trip is overkill; accept
        return pdpaAddress ? S_OK : DPNERR_INVALIDPARAM;
    }
    STDMETHODIMP IsEqual(IDirectPlay8Address* pdpaAddress) override {
        return pdpaAddress == static_cast<IDirectPlay8Address*>(this) ? S_OK : S_FALSE;
    }
    STDMETHODIMP Clear() override { m_comps.clear(); m_hasSP = m_hasDevice = false; return S_OK; }
    STDMETHODIMP GetUserData(void*, DWORD*) override { return DPNERR_DOESNOTEXIST; }
    STDMETHODIMP SetUserData(const void*, DWORD) override { return S_OK; }
    STDMETHODIMP BuildFromDirectPlay4Address(void*, DWORD) override { return DPNERR_UNSUPPORTED; }

private:
    struct Comp { std::wstring name; DWORD type; std::wstring str; DWORD dword = 0; };

    HRESULT CopyComponent(Comp& c, void* buf, DWORD* size, DWORD* type) {
        if (type) *type = c.type;
        if (c.type == DPNA_DATATYPE_DWORD) {
            if (!buf || !size || *size < sizeof(DWORD)) { if (size) *size = sizeof(DWORD); return DPNERR_BUFFERTOOSMALL; }
            *reinterpret_cast<DWORD*>(buf) = c.dword; *size = sizeof(DWORD);
        } else {
            DWORD need = (DWORD)(c.str.size() + 1) * sizeof(WCHAR);
            if (!buf || !size || *size < need) { if (size) *size = need; return DPNERR_BUFFERTOOSMALL; }
            wcscpy_s(reinterpret_cast<WCHAR*>(buf), *size / sizeof(WCHAR), c.str.c_str());
            *size = need;
        }
        return S_OK;
    }

    std::wstring BuildURL() {
        std::wstring url = L"x-directplay:/";
        if (m_hasSP) {
            wchar_t g[64];
            swprintf_s(g, L"provider=%%7B%08lX-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X%%7D",
                       m_sp.Data1, m_sp.Data2, m_sp.Data3, m_sp.Data4[0], m_sp.Data4[1],
                       m_sp.Data4[2], m_sp.Data4[3], m_sp.Data4[4], m_sp.Data4[5],
                       m_sp.Data4[6], m_sp.Data4[7]);
            url += g;
        }
        for (auto& c : m_comps) {
            url += L";" + c.name + L"=";
            if (c.type == DPNA_DATATYPE_DWORD) { wchar_t n[16]; swprintf_s(n, L"%lu", c.dword); url += n; }
            else url += c.str;
        }
        return url;
    }

    HRESULT ParseURL(const std::wstring& url) {
        Clear();
        size_t slash = url.find(L":/");
        std::wstring body = slash == std::wstring::npos ? url : url.substr(slash + 2);
        // components separated by ';', each key=value
        size_t pos = 0;
        while (pos < body.size()) {
            size_t semi = body.find(L';', pos);
            std::wstring tok = body.substr(pos, semi == std::wstring::npos ? std::wstring::npos : semi - pos);
            size_t eq = tok.find(L'=');
            if (eq != std::wstring::npos) {
                std::wstring key = tok.substr(0, eq), val = tok.substr(eq + 1);
                if (key == L"provider") { /* SP guid encoded; loopback ignores it */ }
                else if (key == L"port") { Comp c; c.name = key; c.type = DPNA_DATATYPE_DWORD; c.dword = _wtoi(val.c_str()); m_comps.push_back(c); }
                else { Comp c; c.name = key; c.type = DPNA_DATATYPE_STRING; c.str = val; m_comps.push_back(c); }
            }
            if (semi == std::wstring::npos) break;
            pos = semi + 1;
        }
        return S_OK;
    }

    LONG m_ref;
    GUID m_sp{}; bool m_hasSP = false;
    GUID m_device{}; bool m_hasDevice = false;
    std::vector<Comp> m_comps;
};

// ===================================================================
//  IDirectPlay8Peer  (loopback host)
// ===================================================================

class ReplPeer : public IDirectPlay8Peer {
public:
    ReplPeer() : m_ref(1) { InterlockedIncrement(&g_objects); InitializeCriticalSection(&m_lock); }
    ~ReplPeer() {
        // Always join the worker before tearing down the state it touches,
        // even if the game Released without calling Close() (abort paths).
        // StopWorker() is idempotent, so a prior Close() makes this a no-op.
        StopWorker();
        for (auto& kv : m_kept) free(kv.second);
        m_kept.clear();
        DeleteCriticalSection(&m_lock);
    }

    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (IsEqualIID(riid, IID_IUnknown) || IsEqualIID(riid, IID_IDirectPlay8Peer)) {
            *ppv = static_cast<IDirectPlay8Peer*>(this); AddRef(); return S_OK;
        }
        *ppv = NULL; return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&m_ref); }
    STDMETHODIMP_(ULONG) Release() override {
        LONG r = InterlockedDecrement(&m_ref);
        if (r == 0) { InterlockedDecrement(&g_objects); delete this; }
        return r;
    }

    STDMETHODIMP Initialize(PVOID pvUserContext, PFNDPNMESSAGEHANDLER pfn, DWORD) override {
        m_handler = pfn; m_context = pvUserContext;
        StartWorker();
        Log("Peer::Initialize");
        return S_OK;
    }

    STDMETHODIMP EnumServiceProviders(const GUID*, const GUID*,
                                      DPN_SERVICE_PROVIDER_INFO* pInfo,
                                      DWORD* pcbEnumData, DWORD* pcReturned,
                                      DWORD) override {
        // One entry: the TCP/IP service provider. Two-call size pattern.
        static const wchar_t kName[] = L"DirectPlay8 TCP/IP Service Provider";
        DWORD need = sizeof(DPN_SERVICE_PROVIDER_INFO) + sizeof(kName);
        if (pcReturned) *pcReturned = 1;
        if (!pInfo || !pcbEnumData || *pcbEnumData < need) {
            if (pcbEnumData) *pcbEnumData = need;
            Log("Peer::EnumServiceProviders (size query)");
            return DPNERR_BUFFERTOOSMALL;
        }
        WCHAR* strDst = reinterpret_cast<WCHAR*>(reinterpret_cast<BYTE*>(pInfo) + sizeof(DPN_SERVICE_PROVIDER_INFO));
        wcscpy_s(strDst, sizeof(kName) / sizeof(WCHAR), kName);
        pInfo->dwFlags = 0; pInfo->guid = CLSID_DP8SP_TCPIP;
        pInfo->pwszName = strDst; pInfo->pvReserved = NULL; pInfo->dwReserved = 0;
        *pcbEnumData = need;
        Log("Peer::EnumServiceProviders (fill)");
        return S_OK;
    }

    STDMETHODIMP GetSPCaps(const GUID*, DPN_SP_CAPS* pCaps, DWORD) override {
        if (!pCaps || pCaps->dwSize < sizeof(DPN_SP_CAPS)) return DPNERR_INVALIDPARAM;
        pCaps->dwFlags = 0;
        pCaps->dwNumThreads = 2;
        pCaps->dwDefaultEnumCount = 5;
        pCaps->dwDefaultEnumRetryInterval = 1000;
        pCaps->dwDefaultEnumTimeout = 1000;
        pCaps->dwMaxEnumPayloadSize = 983;
        pCaps->dwBuffersPerThread = 1;
        pCaps->dwSystemBufferSize = 0x10000;
        Log("Peer::GetSPCaps");
        return S_OK;
    }

    STDMETHODIMP SetPeerInfo(const DPN_PLAYER_INFO* pInfo, PVOID, DPNHANDLE*, DWORD) override {
        if (pInfo && pInfo->pwszName) m_playerName = pInfo->pwszName;
        Log("Peer::SetPeerInfo (%S)", m_playerName.c_str());
        return S_OK;
    }

    STDMETHODIMP Host(const DPN_APPLICATION_DESC*, IDirectPlay8Address**, DWORD,
                      const DPN_SECURITY_DESC*, const DPN_SECURITY_CREDENTIALS*,
                      void* pvPlayerContext, DWORD) override {
        m_playerContext = pvPlayerContext;
        m_hosting = true;
        Log("Peer::Host -> local player %lu", LOCAL_PLAYER);
        // The local player is created asynchronously on the worker thread.
        Job j; j.type = Job::CREATE_PLAYER;
        Enqueue(j);
        return S_OK;
    }

    STDMETHODIMP GetPeerInfo(DPNID, DPN_PLAYER_INFO* pInfo, DWORD* pdwSize, DWORD) override {
        DWORD need = sizeof(DPN_PLAYER_INFO) + (DWORD)(m_playerName.size() + 1) * sizeof(WCHAR);
        if (!pInfo || !pdwSize || *pdwSize < need) {
            if (pdwSize) *pdwSize = need;
            return DPNERR_BUFFERTOOSMALL;
        }
        WCHAR* nameDst = reinterpret_cast<WCHAR*>(reinterpret_cast<BYTE*>(pInfo) + sizeof(DPN_PLAYER_INFO));
        wcscpy_s(nameDst, m_playerName.size() + 1, m_playerName.c_str());
        pInfo->dwSize = sizeof(DPN_PLAYER_INFO);
        pInfo->dwInfoFlags = DPNINFO_NAME;
        pInfo->pwszName = nameDst;
        pInfo->pvData = NULL; pInfo->dwDataSize = 0;
        pInfo->dwPlayerFlags = DPNPLAYER_LOCAL | DPNPLAYER_HOST;
        *pdwSize = need;
        Log("Peer::GetPeerInfo");
        return S_OK;
    }

    STDMETHODIMP GetLocalHostAddresses(IDirectPlay8Address** prgpAddress,
                                       DWORD* pcAddress, DWORD) override {
        if (!pcAddress) return DPNERR_INVALIDPARAM;
        if (!prgpAddress || *pcAddress < 1) { *pcAddress = 1; return DPNERR_BUFFERTOOSMALL; }
        ReplAddress* a = new ReplAddress();
        a->SetSP(&CLSID_DP8SP_TCPIP);
        DWORD host = 0; const wchar_t* hn = L"127.0.0.1";
        a->AddComponent(DPNA_KEY_HOSTNAME, hn, (DWORD)(wcslen(hn) + 1) * sizeof(WCHAR), DPNA_DATATYPE_STRING);
        DWORD port = 57033;
        a->AddComponent(DPNA_KEY_PORT, &port, sizeof(port), DPNA_DATATYPE_DWORD);
        (void)host;
        prgpAddress[0] = a;
        *pcAddress = 1;
        Log("Peer::GetLocalHostAddresses");
        return S_OK;
    }

    STDMETHODIMP SendTo(DPNID, const DPN_BUFFER_DESC* prgBufferDesc, DWORD cBufferDesc,
                        DWORD, void* pvAsyncContext, DPNHANDLE* phAsyncHandle,
                        DWORD dwFlags) override {
        // Concatenate the buffers and loop them back to the local player as
        // a RECEIVE. If async, complete the send with SEND_COMPLETE first.
        std::vector<BYTE> data;
        for (DWORD i = 0; i < cBufferDesc; ++i)
            data.insert(data.end(), prgBufferDesc[i].pBufferData,
                        prgBufferDesc[i].pBufferData + prgBufferDesc[i].dwBufferSize);

        bool async = (phAsyncHandle != NULL) && !(dwFlags & DPNSEND_SYNC);
        DPNHANDLE h = 0;
        if (async) { h = NextHandle(); if (phAsyncHandle) *phAsyncHandle = h; }

        if (async) {
            Job sc; sc.type = Job::SEND_COMPLETE; sc.handle = h; sc.context = pvAsyncContext;
            Enqueue(sc);
        }
        Job rc; rc.type = Job::RECEIVE; rc.data.swap(data);
        Enqueue(rc);
        return async ? DPNSUCCESS_PENDING : S_OK;
    }

    STDMETHODIMP ReturnBuffer(DPNHANDLE hBufferHandle, DWORD) override {
        EnterCriticalSection(&m_lock);
        auto it = m_kept.find(hBufferHandle);
        if (it != m_kept.end()) { free(it->second); m_kept.erase(it); }
        LeaveCriticalSection(&m_lock);
        return S_OK;
    }

    STDMETHODIMP Close(DWORD) override {
        Log("Peer::Close");
        if (m_hosting) { Job j; j.type = Job::DESTROY_PLAYER; Enqueue(j); m_hosting = false; }
        StopWorker();
        // free any buffers the game never returned
        EnterCriticalSection(&m_lock);
        for (auto& kv : m_kept) free(kv.second);
        m_kept.clear();
        LeaveCriticalSection(&m_lock);
        return S_OK;
    }

#include "peer_stubs.inc"

private:
    struct Job {
        enum Type { CREATE_PLAYER, DESTROY_PLAYER, RECEIVE, SEND_COMPLETE } type;
        std::vector<BYTE> data;   // RECEIVE payload
        DPNHANDLE handle = 0;     // SEND_COMPLETE async handle
        void* context = NULL;     // SEND_COMPLETE user context
    };

    void StartWorker() {
        m_stop = false;
        m_wake = CreateEventW(NULL, FALSE, FALSE, NULL);
        m_thread = CreateThread(NULL, 0, &ReplPeer::ThreadEntry, this, 0, NULL);
    }
    void StopWorker() {
        if (!m_thread) return;
        m_stop = true; SetEvent(m_wake);
        // Wait until the worker has provably exited before freeing the
        // handles and state it uses - a bounded wait could abandon a still
        // running thread and let it touch freed memory. The loopback
        // handler always returns promptly, so this cannot hang in practice.
        WaitForSingleObject(m_thread, INFINITE);
        CloseHandle(m_thread); m_thread = NULL;
        CloseHandle(m_wake); m_wake = NULL;
    }
    void Enqueue(Job& j) {
        EnterCriticalSection(&m_lock);
        m_queue.push_back(std::move(j));
        LeaveCriticalSection(&m_lock);
        if (m_wake) SetEvent(m_wake);
    }
    DPNHANDLE NextHandle() { return (DPNHANDLE)InterlockedIncrement(&m_nextHandle); }

    static DWORD WINAPI ThreadEntry(LPVOID p) { ((ReplPeer*)p)->WorkerLoop(); return 0; }

    void WorkerLoop() {
        for (;;) {
            WaitForSingleObject(m_wake, 100);
            for (;;) {
                Job j;
                EnterCriticalSection(&m_lock);
                if (m_queue.empty()) { LeaveCriticalSection(&m_lock); break; }
                j = std::move(m_queue.front()); m_queue.pop_front();
                LeaveCriticalSection(&m_lock);
                Dispatch(j);
            }
            if (m_stop) break;
        }
    }

    void Dispatch(Job& j) {
        if (!m_handler) return;
        switch (j.type) {
        case Job::CREATE_PLAYER: {
            DPNMSG_CREATE_PLAYER m{}; m.dwSize = sizeof(m);
            m.dpnidPlayer = LOCAL_PLAYER; m.pvPlayerContext = m_playerContext;
            m_handler(m_context, DPN_MSGID_CREATE_PLAYER, &m);
            break;
        }
        case Job::DESTROY_PLAYER: {
            DPNMSG_DESTROY_PLAYER m{}; m.dwSize = sizeof(m);
            m.dpnidPlayer = LOCAL_PLAYER; m.pvPlayerContext = m_playerContext;
            m.dwReason = DPNDESTROYPLAYERREASON_NORMAL;
            m_handler(m_context, DPN_MSGID_DESTROY_PLAYER, &m);
            break;
        }
        case Job::SEND_COMPLETE: {
            DPNMSG_SEND_COMPLETE m{}; m.dwSize = sizeof(m);
            m.hAsyncOp = j.handle; m.pvUserContext = j.context;
            m.hResultCode = S_OK; m.dwSendTime = 0;
            m_handler(m_context, DPN_MSGID_SEND_COMPLETE, &m);
            break;
        }
        case Job::RECEIVE: {
            DWORD size = (DWORD)j.data.size();
            BYTE* buf = (BYTE*)malloc(size ? size : 1);
            if (size) memcpy(buf, j.data.data(), size);
            DPNHANDLE h = NextHandle();
            DPNMSG_RECEIVE m{}; m.dwSize = sizeof(m);
            m.dpnidSender = LOCAL_PLAYER; m.pvPlayerContext = m_playerContext;
            m.pReceiveData = buf; m.dwReceiveDataSize = size; m.hBufferHandle = h;
            HRESULT hr = m_handler(m_context, DPN_MSGID_RECEIVE, &m);
            if (hr == DPNSUCCESS_PENDING) {
                EnterCriticalSection(&m_lock);
                m_kept[h] = buf;   // game keeps it; freed on ReturnBuffer
                LeaveCriticalSection(&m_lock);
            } else {
                free(buf);         // handler consumed it synchronously
            }
            break;
        }
        }
    }

    LONG m_ref;
    PFNDPNMESSAGEHANDLER m_handler = NULL;
    PVOID m_context = NULL;
    PVOID m_playerContext = NULL;
    std::wstring m_playerName = L"Player";
    bool m_hosting = false;

    CRITICAL_SECTION m_lock;
    std::deque<Job> m_queue;
    std::map<DPNHANDLE, void*> m_kept;
    HANDLE m_thread = NULL;
    HANDLE m_wake = NULL;
    volatile bool m_stop = false;
    volatile LONG m_nextHandle = 1000;
};

// ===================================================================
//  class factories + exports
// ===================================================================

template <class T>
class Factory : public IClassFactory {
public:
    Factory() : m_ref(1) {}
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (IsEqualIID(riid, IID_IUnknown) || IsEqualIID(riid, IID_IClassFactory)) {
            *ppv = this; AddRef(); return S_OK;
        }
        *ppv = NULL; return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&m_ref); }
    STDMETHODIMP_(ULONG) Release() override {
        LONG r = InterlockedDecrement(&m_ref); if (r == 0) delete this; return r;
    }
    STDMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** ppv) override {
        if (outer) return CLASS_E_NOAGGREGATION;
        T* obj = new T();
        HRESULT hr = obj->QueryInterface(riid, ppv);
        obj->Release();
        return hr;
    }
    STDMETHODIMP LockServer(BOOL) override { return S_OK; }
private:
    LONG m_ref;
};

STDAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, LPVOID* ppv) {
    LogInit();
    if (IsEqualGUID(rclsid, CLSID_DirectPlay8Peer)) {
        Log("DllGetClassObject: Peer");
        Factory<ReplPeer>* f = new Factory<ReplPeer>();
        HRESULT hr = f->QueryInterface(riid, ppv); f->Release(); return hr;
    }
    if (IsEqualGUID(rclsid, CLSID_DirectPlay8Address)) {
        Log("DllGetClassObject: Address");
        Factory<ReplAddress>* f = new Factory<ReplAddress>();
        HRESULT hr = f->QueryInterface(riid, ppv); f->Release(); return hr;
    }
    Log("DllGetClassObject: unknown clsid {%08lX-...}", rclsid.Data1);
    return CLASS_E_CLASSNOTAVAILABLE;
}

STDAPI DllCanUnloadNow(void) { return g_objects == 0 ? S_OK : S_FALSE; }

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(inst);
        LogInit();
        Log("=== dpnetreplace geladen (pid %lu) ===", GetCurrentProcessId());
    }
    return TRUE;
}
