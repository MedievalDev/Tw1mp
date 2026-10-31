// Self-test for the capture shim: loads dpnetcapture.dll directly (no
// registry), which in turn loads the real Windows dpnet.dll, and runs the
// solo-host sequence Two Worlds uses (Initialize, EnumServiceProviders,
// SetPeerInfo, Host, GetLocalHostAddresses, SendTo to self, Close).
// Then it checks that dpnetcapture.log contains the argument-level detail
// the two-player capture depends on. Exit code 0 = PASS.

#define WIN32_LEAN_AND_MEAN
#define INITGUID
#include <windows.h>
#include <dplay8.h>
#include <dpaddr.h>
#include <stdio.h>
#include <share.h>
#include <string>
#include <vector>

typedef HRESULT (WINAPI *PFN_DllGetClassObject)(REFCLSID, REFIID, LPVOID*);

static volatile LONG g_created = 0, g_received = 0;

static HRESULT WINAPI Handler(PVOID, DWORD id, PVOID msg) {
    if (id == DPN_MSGID_CREATE_PLAYER) InterlockedIncrement(&g_created);
    if (id == DPN_MSGID_RECEIVE) InterlockedIncrement(&g_received);
    return S_OK;
}

static bool Contains(const std::string& hay, const char* needle) {
    return hay.find(needle) != std::string::npos;
}

int main() {
    CoInitializeEx(NULL, COINIT_MULTITHREADED);
    HMODULE dll = LoadLibraryA("dpnetcapture.dll");
    if (!dll) { printf("LoadLibrary dpnetcapture.dll failed %lu\n", GetLastError()); return 1; }
    PFN_DllGetClassObject dgco = (PFN_DllGetClassObject)GetProcAddress(dll, "DllGetClassObject");

    IClassFactory* pf = NULL;
    if (FAILED(dgco(CLSID_DirectPlay8Peer, IID_IClassFactory, (void**)&pf))) { printf("peer factory failed\n"); return 1; }
    IDirectPlay8Peer* peer = NULL;
    HRESULT hr = pf->CreateInstance(NULL, IID_IDirectPlay8Peer, (void**)&peer);
    pf->Release();
    if (FAILED(hr) || !peer) { printf("CreateInstance peer failed 0x%08lX\n", hr); return 1; }

    IClassFactory* af = NULL;
    dgco(CLSID_DirectPlay8Address, IID_IClassFactory, (void**)&af);
    IDirectPlay8Address* dev = NULL;
    af->CreateInstance(NULL, IID_IDirectPlay8Address, (void**)&dev);
    af->Release();

    hr = peer->Initialize(NULL, Handler, 0);
    printf("Initialize -> 0x%08lX\n", hr);

    DWORD cb = 0, count = 0;
    peer->EnumServiceProviders(NULL, NULL, NULL, &cb, &count, 0);
    std::vector<BYTE> buf(cb ? cb : 1);
    hr = peer->EnumServiceProviders(NULL, NULL, (DPN_SERVICE_PROVIDER_INFO*)buf.data(), &cb, &count, 0);
    printf("EnumServiceProviders -> 0x%08lX count=%lu\n", hr, count);

    dev->SetSP(&CLSID_DP8SP_TCPIP);

    DPN_PLAYER_INFO pi{}; pi.dwSize = sizeof(pi); pi.dwInfoFlags = DPNINFO_NAME; pi.pwszName = (WCHAR*)L"capturetest";
    hr = peer->SetPeerInfo(&pi, NULL, NULL, DPNSETPEERINFO_SYNC);
    printf("SetPeerInfo -> 0x%08lX\n", hr);

    DPN_APPLICATION_DESC app{}; app.dwSize = sizeof(app);
    app.dwFlags = DPNSESSION_NODPNSVR; app.dwMaxPlayers = 8;
    app.pwszSessionName = (WCHAR*)L"capture-selftest";
    static const GUID appGuid = { 0x9c48a3e2, 0x1b2c, 0x4d5e, { 0x8f, 0x90, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66 } };
    app.guidApplication = appGuid;
    hr = peer->Host(&app, &dev, 1, NULL, NULL, NULL, 0);
    printf("Host -> 0x%08lX\n", hr);
    bool hosted = SUCCEEDED(hr);

    if (hosted) {
        IDirectPlay8Address* addrs[8] = {0}; DWORD na = 8;
        hr = peer->GetLocalHostAddresses(addrs, &na, 0);
        printf("GetLocalHostAddresses -> 0x%08lX count=%lu\n", hr, na);
        for (DWORD i = 0; i < na; ++i) {
            WCHAR url[512]; DWORD ul = 512;
            if (addrs[i] && SUCCEEDED(addrs[i]->GetURLW(url, &ul))) printf("  %S\n", url);
            if (addrs[i]) addrs[i]->Release();
        }
        const char* payload = "hello-self";
        DPN_BUFFER_DESC bd{}; bd.dwBufferSize = (DWORD)strlen(payload); bd.pBufferData = (BYTE*)payload;
        DPNHANDLE h = 0;
        hr = peer->SendTo(DPNID_ALL_PLAYERS_GROUP, &bd, 1, 0, NULL, &h, DPNSEND_GUARANTEED);
        printf("SendTo(ALL, guaranteed) -> 0x%08lX\n", hr);
        for (int i = 0; i < 50 && !g_received; ++i) Sleep(20);
        printf("loopback RECEIVE seen: %s\n", g_received ? "yes" : "no");
        hr = peer->SendTo(DPNID_ALL_PLAYERS_GROUP, &bd, 1, 0, NULL, &h, DPNSEND_GUARANTEED | DPNSEND_NOLOOPBACK);
        printf("SendTo(ALL, guaranteed|noloopback) -> 0x%08lX\n", hr);
        Sleep(200);
        peer->Close(0);
    }
    dev->Release();
    peer->Release();

    // Verify the log carries the argument-level detail.
    char path[MAX_PATH]; GetModuleFileNameA(dll, path, MAX_PATH);
    char* slash = strrchr(path, '\\'); if (slash) strcpy_s(slash + 1, MAX_PATH - (slash + 1 - path), "dpnetcapture.log");
    // The DLL keeps the log open (shared); read it with sharing allowed too.
    FILE* f = _fsopen(path, "rb", _SH_DENYNO);
    std::string log;
    if (f) { char b[4096]; size_t n; while ((n = fread(b, 1, sizeof(b), f)) > 0) log.append(b, n); fclose(f); }

    struct Check { const char* what; const char* needle; } checks[] = {
        { "Initialize logged with flags",      "CALL Peer::Initialize flags=" },
        { "SetPeerInfo logs the name",         "name='capturetest'" },
        { "Host logs the app desc",            "CALL Peer::Host appdesc{" },
        { "Host app desc has session name",    "name='capture-selftest'" },
        { "GetLocalHostAddresses logs URLs",   "CALL Peer::GetLocalHostAddresses" },
        { "SendTo logs flags and hex",         "flags=GUARANTEED(0x8)" },
        { "SendTo NOLOOPBACK decoded",         "GUARANTEED|NOLOOPBACK" },
        { "SendTo hex prefix present",         "hex=68656C6C6F2D73656C66" },
        { "CREATE_PLAYER dpnid logged",        "MSG CREATE_PLAYER dpnid=" },
        { "message return value logged",       "-> game returned S_OK" },
        { "Close logged with result",          "CALL Peer::Close -> " },
        { "thread id on lines",                " t=" },
    };
    bool pass = hosted && g_created > 0;
    printf("\nhost ok=%d create_player=%ld receive=%ld\n", hosted, g_created, g_received);
    for (auto& c : checks) {
        bool ok = Contains(log, c.needle);
        printf("  [%s] %s\n", ok ? "OK" : "MISSING", c.what);
        if (!ok) pass = false;
    }
    if (hosted && !Contains(log, "MSG RECEIVE from="))
        printf("  [NOTE] no RECEIVE in log (stock dpnet may not loop back to self here)\n");
    printf("\n=== RESULT: %s ===\n", pass ? "PASS" : "FAIL");
    CoUninitialize();
    return pass ? 0 : 2;
}
