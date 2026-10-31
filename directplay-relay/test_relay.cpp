// Self-test for the relay replacement: loads dpnetrelay.dll directly (no
// registry), creates a host and two joiners in one process and drives them
// through a relay whose address comes from the TW1MP_RELAY environment
// variable (tests/test_relay_dll.py starts one and runs this). Exercises:
//
//   host session + advertised address with a session id
//   joiner address round-trip through a URL string (as the game does)
//   two joiners: CREATE_PLAYER fan-out on every side, roster in the welcome
//   broadcast with/without loopback, directed send
//   guaranteed delivery in order under 30% simulated loss (per peer)
//   player info / enumeration / contexts
//   CREATE_PLAYER for the host inside Host(), measured round trip
//   joiner refused in INDICATE_CONNECT -> HOSTREJECTEDCONNECTION + reply data,
//     RETURN_BUFFER at the host, no player anywhere
//   raw member that never says hello -> no ghost player
//   NAT address change (new local port) -> rebind, nobody dropped
//   kick with 4 data bytes -> TERMINATE_SESSION with them, DESTROY elsewhere
//   late joiner after a kick, announced to the others by the host
//   joiner leaving -> DESTROY_PLAYER elsewhere; host leaving -> TERMINATE
//   connecting to a session that does not exist -> failed CONNECT_COMPLETE
//
// Extra modes (tests/test_relay_dll.py drives them):
//   relaykill  the relay is stopped mid-session -> joiner TERMINATE, host goes on
//   impaired   relay delays 100 +- 25 ms and drops 5 % -> stream complete, RTT seen
//
// Exit code 0 = PASS.

#define WIN32_LEAN_AND_MEAN
#define INITGUID
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <dplay8.h>
#include <dpaddr.h>
#include <stdio.h>
#include <string>
#include <set>
#include <vector>
#include <map>

typedef HRESULT (WINAPI *PFN_DllGetClassObject)(REFCLSID, REFIID, LPVOID*);
typedef void (*PFN_SetInt)(int);

struct Rec {
    const char* name;
    CRITICAL_SECTION lock;
    std::set<DPNID> created, destroyed;
    std::map<DPNID, DWORD> destroyReason;
    std::map<DPNID, void*> destroyCtx;
    bool connectDone = false; HRESULT connectHr = E_FAIL; DPNID localId = 0;
    bool terminated = false; HRESULT terminateHr = S_OK;
    int destroyedBeforeTerminate = 0;
    std::vector<std::pair<DPNID, std::string>> msgs;
    LONG relNext = 0; LONG relBad = 0;
    int indicate = 0;
    int sendCompletes = 0;
    bool enumResponse = false;
    bool asyncDone = false; HRESULT asyncHr = S_OK; DPNHANDLE asyncOp = 0;
    bool reject = false;               // host: refuse INDICATE_CONNECT like the game does in a running match
    BYTE replyBuf[24] = {0};
    int returnBuffers = 0; void* returnedCtx = NULL;
    DWORD replySize = 0; DWORD replyRejected = 0;   // joiner: reply data seen in CONNECT_COMPLETE
    std::string terminateData;
    Rec(const char* n) : name(n) { InitializeCriticalSection(&lock); }
};

static HRESULT WINAPI Handler(PVOID ctx, DWORD id, PVOID msg) {
    Rec* r = (Rec*)ctx;
    EnterCriticalSection(&r->lock);
    switch (id) {
    case DPN_MSGID_CREATE_PLAYER: {
        DPNMSG_CREATE_PLAYER* m = (DPNMSG_CREATE_PLAYER*)msg;
        r->created.insert(m->dpnidPlayer);
        m->pvPlayerContext = (void*)(ULONG_PTR)(0x1000 + m->dpnidPlayer);   // like a game would
        printf("[%s] CREATE_PLAYER %lu\n", r->name, m->dpnidPlayer);
        break; }
    case DPN_MSGID_DESTROY_PLAYER: {
        DPNMSG_DESTROY_PLAYER* m = (DPNMSG_DESTROY_PLAYER*)msg;
        r->destroyed.insert(m->dpnidPlayer); r->destroyReason[m->dpnidPlayer] = m->dwReason;
        r->destroyCtx[m->dpnidPlayer] = m->pvPlayerContext;
        if (!r->terminated) r->destroyedBeforeTerminate++;
        printf("[%s] DESTROY_PLAYER %lu reason=%lu ctx=%p\n", r->name, m->dpnidPlayer, m->dwReason, m->pvPlayerContext);
        break; }
    case DPN_MSGID_CONNECT_COMPLETE: {
        DPNMSG_CONNECT_COMPLETE* m = (DPNMSG_CONNECT_COMPLETE*)msg;
        r->connectDone = true; r->connectHr = m->hResultCode; r->localId = m->dpnidLocal;
        r->replySize = m->dwApplicationReplyDataSize;
        if (m->pvApplicationReplyData && m->dwApplicationReplyDataSize >= 8) r->replyRejected = ((DWORD*)m->pvApplicationReplyData)[1];
        printf("[%s] CONNECT_COMPLETE hr=0x%08lX local=%lu reply=%lu\n", r->name, m->hResultCode, m->dpnidLocal,
               m->dwApplicationReplyDataSize);
        break; }
    case DPN_MSGID_INDICATE_CONNECT: {
        DPNMSG_INDICATE_CONNECT* m = (DPNMSG_INDICATE_CONNECT*)msg;
        r->indicate++;
        printf("[%s] INDICATE_CONNECT%s\n", r->name, r->reject ? " -> refused" : "");
        if (r->reject) {
            // As the game (0x6e3398): 24 bytes {running number, rejected = 1, 16 zero bytes}, return -1.
            ((DWORD*)r->replyBuf)[0] = 7; ((DWORD*)r->replyBuf)[1] = 1;
            m->pvReplyData = r->replyBuf; m->dwReplyDataSize = sizeof(r->replyBuf); m->pvReplyContext = (void*)0x55;
            LeaveCriticalSection(&r->lock);
            return (HRESULT)-1;
        }
        break; }
    case DPN_MSGID_RETURN_BUFFER: {
        DPNMSG_RETURN_BUFFER* m = (DPNMSG_RETURN_BUFFER*)msg;
        r->returnBuffers++; r->returnedCtx = m->pvUserContext;
        printf("[%s] RETURN_BUFFER ctx=%p\n", r->name, m->pvUserContext);
        break; }
    case DPN_MSGID_SEND_COMPLETE:
        r->sendCompletes++;
        break;
    case DPN_MSGID_ENUM_HOSTS_RESPONSE:
        r->enumResponse = true;
        printf("[%s] ENUM_HOSTS_RESPONSE\n", r->name);
        break;
    case DPN_MSGID_ASYNC_OP_COMPLETE: {
        DPNMSG_ASYNC_OP_COMPLETE* m = (DPNMSG_ASYNC_OP_COMPLETE*)msg;
        r->asyncDone = true; r->asyncHr = m->hResultCode; r->asyncOp = m->hAsyncOp;
        printf("[%s] ASYNC_OP_COMPLETE op=0x%lX hr=0x%08lX\n", r->name, (DWORD)m->hAsyncOp, m->hResultCode);
        break; }
    case DPN_MSGID_TERMINATE_SESSION: {
        DPNMSG_TERMINATE_SESSION* m = (DPNMSG_TERMINATE_SESSION*)msg;
        r->terminated = true; r->terminateHr = m->hResultCode;
        if (m->pvTerminateData) r->terminateData.assign((char*)m->pvTerminateData, m->dwTerminateDataSize);
        printf("[%s] TERMINATE_SESSION hr=0x%08lX data=%lu\n", r->name, m->hResultCode, m->dwTerminateDataSize);
        break; }
    case DPN_MSGID_RECEIVE: {
        DPNMSG_RECEIVE* m = (DPNMSG_RECEIVE*)msg;
        std::string s((char*)m->pReceiveData, m->dwReceiveDataSize);
        if (!s.empty() && s[0] == 'R') {
            int seq = atoi(s.c_str() + 1);
            if (seq == r->relNext) r->relNext++; else r->relBad++;
        } else {
            r->msgs.push_back(std::make_pair(m->dpnidSender, s));
            printf("[%s] RECEIVE from %lu: %s\n", r->name, m->dpnidSender, s.c_str());
        }
        break; }
    default: break;
    }
    LeaveCriticalSection(&r->lock);
    return S_OK;
}

static PFN_DllGetClassObject g_dgco;

static IDirectPlay8Peer* MakePeer() {
    IClassFactory* f = NULL;
    if (FAILED(g_dgco(CLSID_DirectPlay8Peer, IID_IClassFactory, (void**)&f))) return NULL;
    IDirectPlay8Peer* p = NULL;
    f->CreateInstance(NULL, IID_IDirectPlay8Peer, (void**)&p);
    f->Release();
    return p;
}
static IDirectPlay8Address* MakeAddr() {
    IClassFactory* f = NULL;
    if (FAILED(g_dgco(CLSID_DirectPlay8Address, IID_IClassFactory, (void**)&f))) return NULL;
    IDirectPlay8Address* a = NULL;
    f->CreateInstance(NULL, IID_IDirectPlay8Address, (void**)&a);
    f->Release();
    return a;
}

template <class F> static bool WaitUntil(F f, int ms) {
    for (int i = 0; i < ms / 20; ++i) { if (f()) return true; Sleep(20); }
    return f();
}
static bool HasMsg(Rec& r, DPNID from, const char* text) {
    EnterCriticalSection(&r.lock);
    bool ok = false;
    for (auto& m : r.msgs) if (m.first == from && m.second == text) { ok = true; break; }
    LeaveCriticalSection(&r.lock);
    return ok;
}
static bool Created(Rec& r, DPNID id) { EnterCriticalSection(&r.lock); bool ok = r.created.count(id) != 0; LeaveCriticalSection(&r.lock); return ok; }
static bool Destroyed(Rec& r, DPNID id) { EnterCriticalSection(&r.lock); bool ok = r.destroyed.count(id) != 0; LeaveCriticalSection(&r.lock); return ok; }
static void SetName(IDirectPlay8Peer* p, const WCHAR* n) {
    DPN_PLAYER_INFO pi{}; pi.dwSize = sizeof(pi); pi.dwInfoFlags = DPNINFO_NAME; pi.pwszName = (WCHAR*)n;
    p->SetPeerInfo(&pi, NULL, NULL, DPNSETPEERINFO_SYNC);
}
static void Send(IDirectPlay8Peer* p, DPNID to, const char* text, DWORD flags) {
    DPN_BUFFER_DESC b{}; b.dwBufferSize = (DWORD)strlen(text); b.pBufferData = (BYTE*)text;
    DPNHANDLE h = 0;
    p->SendTo(to, &b, 1, 0, NULL, &h, flags);
}

static int g_fails = 0;
static void Check(bool ok, const char* what) {
    printf("  [%s] %s\n", ok ? "OK" : "FAIL", what);
    if (!ok) g_fails++;
}

// -- raw relay member (stands in for a client that never says IN_HELLO) ------

#pragma pack(push, 1)
struct RlHdr { DWORD magic; BYTE type; DWORD session; WORD src; WORD dst; };
#pragma pack(pop)

static bool RelaySockAddr(const char* relay, sockaddr_in& out) {
    std::string s = relay; size_t c = s.rfind(':');
    if (c == std::string::npos) return false;
    out = sockaddr_in{}; out.sin_family = AF_INET; out.sin_port = htons((u_short)atoi(s.c_str() + c + 1));
    return inet_pton(AF_INET, s.substr(0, c).c_str(), &out.sin_addr) == 1;
}
static void RawSend(SOCKET s, const sockaddr_in& to, BYTE type, DWORD session, WORD src, WORD dst, const void* p, int n) {
    std::vector<BYTE> pkt(sizeof(RlHdr) + n);
    RlHdr* h = (RlHdr*)pkt.data(); h->magic = 0x4C525754; h->type = type; h->session = session; h->src = src; h->dst = dst;
    if (n) memcpy(pkt.data() + sizeof(RlHdr), p, n);
    sendto(s, (char*)pkt.data(), (int)pkt.size(), 0, (const sockaddr*)&to, sizeof(to));
}

static DWORD SessionOf(const char* url) {
    const char* p = strstr(url, ";session=");
    return p ? strtoul(p + 9, NULL, 16) : 0;
}

static PFN_SetInt g_setDrop, g_setJoinTimeout, g_setRelaySilence, g_setPeerLost;
typedef void (*PFN_Rebind)(IDirectPlay8Peer*);
static PFN_Rebind g_rebind;

static IDirectPlay8Peer* HostSession(Rec& rh, char* url, DWORD urlSize, DPN_APPLICATION_DESC& app) {
    IDirectPlay8Peer* host = MakePeer();
    host->Initialize(&rh, Handler, 0); SetName(host, L"Host");
    app = DPN_APPLICATION_DESC{}; app.dwSize = sizeof(app); app.dwMaxPlayers = 8;
    app.pwszSessionName = (WCHAR*)L"relay-selftest";
    HRESULT hr = host->Host(&app, NULL, 0, NULL, NULL, (void*)0x77, 0);
    Check(hr == S_OK, "Host() succeeds");
    IDirectPlay8Address* addrs[4] = {0}; DWORD na = 4;
    hr = host->GetLocalHostAddresses(addrs, &na, 0);
    Check(hr == S_OK && na == 1, "GetLocalHostAddresses returns one address");
    DWORD ul = urlSize;
    if (na) { addrs[0]->GetURLA(url, &ul); addrs[0]->Release(); }
    printf("  host URL: %s\n", url);
    return host;
}
static IDirectPlay8Peer* JoinSession(Rec& r, const WCHAR* name, const char* url, DPN_APPLICATION_DESC& app, void* ctx) {
    IDirectPlay8Peer* p = MakePeer();
    p->Initialize(&r, Handler, 0); SetName(p, name);
    IDirectPlay8Address* a = MakeAddr(); a->BuildFromURLA((char*)url);
    DPNHANDLE h = 0;
    p->Connect(&app, a, NULL, NULL, NULL, NULL, 0, ctx, NULL, &h, 0);
    a->Release();
    return p;
}

// Mode "relaykill": the Python side stops the relay when told; the joiner
// must get TERMINATE_SESSION, the host keeps its own player.
static int RunRelayKill() {
    Rec rh("HOST"), r1("ALICE");
    char url[1024] = {0}; DPN_APPLICATION_DESC app;
    if (g_setRelaySilence) g_setRelaySilence(3000);
    IDirectPlay8Peer* host = HostSession(rh, url, sizeof(url), app);
    IDirectPlay8Peer* j1 = JoinSession(r1, L"Alice", url, app, NULL);
    Check(WaitUntil([&] { return r1.connectDone; }, 8000) && SUCCEEDED(r1.connectHr), "joiner connected");
    Sleep(2500);   // at least one keepalive echo, so both know the relay echoes
    printf("READY_KILL_RELAY\n"); fflush(stdout);
    Check(WaitUntil([&] { return r1.terminated; }, 12000), "joiner gets TERMINATE_SESSION once the relay is silent");
    Check(r1.terminateHr == DPNERR_CONNECTIONLOST, "TERMINATE_SESSION reports CONNECTIONLOST");
    Check(WaitUntil([&] { return Destroyed(rh, 2); }, 12000), "host loses the joiner (DESTROY_PLAYER 2)");
    Check(rh.destroyReason[2] == DPNDESTROYPLAYERREASON_CONNECTIONLOST, "reason CONNECTIONLOST");
    Check(!Destroyed(rh, 1) && !rh.terminated, "host keeps its own player, no TERMINATE for the host");
    j1->Close(0); host->Close(0);
    j1->Release(); host->Release();
    printf("\n=== RESULT: %s (%d failed) ===\n", g_fails == 0 ? "PASS" : "FAIL", g_fails);
    return g_fails == 0 ? 0 : 2;
}

// Mode "impaired": the relay delays every forwarded packet 100 ms +- 25 ms
// and drops 5 %. A steady game-like stream must arrive complete, in order,
// with no player lost, and the reported round trip must show the delay.
static int RunImpaired() {
    Rec rh("HOST"), r1("ALICE");
    char url[1024] = {0}; DPN_APPLICATION_DESC app;
    if (g_setJoinTimeout) g_setJoinTimeout(10000);
    IDirectPlay8Peer* host = HostSession(rh, url, sizeof(url), app);
    IDirectPlay8Peer* j1 = JoinSession(r1, L"Alice", url, app, NULL);
    Check(WaitUntil([&] { return r1.connectDone; }, 12000) && SUCCEEDED(r1.connectHr), "joiner connected through the slow relay");
    const int N = 300;   // 15 s at 20/s, plus unreliable turn packets in between
    for (int i = 0; i < N; ++i) {
        char t[16]; sprintf_s(t, "R%d", i);
        Send(host, 2, t, DPNSEND_GUARANTEED);
        Send(j1, 1, "U", 0);
        Sleep(50);
    }
    Check(WaitUntil([&] { return r1.relNext >= N; }, 15000), "300 guaranteed messages arrive");
    printf("  alice %ld/%d (bad %ld)\n", r1.relNext, N, r1.relBad);
    Check(r1.relBad == 0, "... exactly once and in order");
    Check(!Destroyed(rh, 2) && !Destroyed(r1, 1) && !r1.terminated, "nobody was dropped");
    DPN_CONNECTION_INFO ci{}; ci.dwSize = sizeof(ci);
    Check(host->GetConnectionInfo(2, &ci, 0) == S_OK, "host GetConnectionInfo(2) ok");
    printf("  host -> alice round trip %lu ms\n", ci.dwRoundTripLatencyMS);
    Check(ci.dwRoundTripLatencyMS >= 150 && ci.dwRoundTripLatencyMS <= 600, "round trip reflects 2 x 100 ms relay delay");
    j1->Close(0); host->Close(0);
    j1->Release(); host->Release();
    printf("\n=== RESULT: %s (%d failed) ===\n", g_fails == 0 ? "PASS" : "FAIL", g_fails);
    return g_fails == 0 ? 0 : 2;
}

int main(int argc, char** argv) {
    char relay[256] = {0};
    if (GetEnvironmentVariableA("TW1MP_RELAY", relay, sizeof(relay)) == 0) {
        printf("TW1MP_RELAY not set (e.g. 127.0.0.1:17172 with 'python -m tw1mp.relay' running)\n");
        return 1;
    }
    printf("relay: %s\n", relay);
    WSADATA wsa; WSAStartup(MAKEWORD(2, 2), &wsa);
    HMODULE dll = LoadLibraryA("dpnetrelay.dll");
    if (!dll) { printf("LoadLibrary dpnetrelay.dll failed %lu\n", GetLastError()); return 1; }
    g_dgco = (PFN_DllGetClassObject)GetProcAddress(dll, "DllGetClassObject");
    g_setDrop = (PFN_SetInt)GetProcAddress(dll, "DpnTestSetDrop");
    g_setJoinTimeout = (PFN_SetInt)GetProcAddress(dll, "DpnTestSetJoinTimeout");
    g_setRelaySilence = (PFN_SetInt)GetProcAddress(dll, "DpnTestSetRelaySilence");
    g_setPeerLost = (PFN_SetInt)GetProcAddress(dll, "DpnTestSetPeerLost");
    g_rebind = (PFN_Rebind)GetProcAddress(dll, "DpnTestRebind");
    std::string mode = argc > 1 ? argv[1] : "";
    if (mode == "relaykill") return RunRelayKill();
    if (mode == "impaired") return RunImpaired();

    Rec rh("HOST"), r1("ALICE"), r2("BOB"), r3("EVE"), r4("DAVE"), r5("MALLORY");
    IDirectPlay8Peer* host = MakePeer();
    IDirectPlay8Peer* j1 = MakePeer();
    IDirectPlay8Peer* j2 = MakePeer();
    if (!host || !j1 || !j2) { printf("MakePeer failed\n"); return 1; }
    host->Initialize(&rh, Handler, 0); j1->Initialize(&r1, Handler, 0); j2->Initialize(&r2, Handler, 0);
    SetName(host, L"Host"); SetName(j1, L"Alice"); SetName(j2, L"Bob");

    // ---- host -------------------------------------------------------------
    printf("\n== hosting ==\n");
    DPN_APPLICATION_DESC app{}; app.dwSize = sizeof(app); app.dwMaxPlayers = 8;
    app.pwszSessionName = (WCHAR*)L"relay-selftest";
    HRESULT hr = host->Host(&app, NULL, 0, NULL, NULL, (void*)0x77, 0);
    Check(hr == S_OK, "Host() succeeds");
    Check(Created(rh, 1), "host's own CREATE_PLAYER (1) arrived inside Host()");
    IDirectPlay8Address* addrs[4] = {0}; DWORD na = 4;
    hr = host->GetLocalHostAddresses(addrs, &na, 0);
    Check(hr == S_OK && na == 1, "GetLocalHostAddresses returns one address");
    char url[1024] = {0}; DWORD ul = sizeof(url);
    if (na) addrs[0]->GetURLA(url, &ul);
    printf("  host URL: %s\n", url);
    Check(strstr(url, ";session=") != NULL, "URL carries a session id");
    Check(strstr(url, ";session=00000000") == NULL, "session id is non-zero (relay answered)");

    // ---- joiner 1: address rebuilt from the URL string, like the game -------
    printf("\n== joiner 1 ==\n");
    IDirectPlay8Address* a1 = MakeAddr();
    Check(a1->BuildFromURLA(url) == S_OK, "BuildFromURLA on the host URL");
    DPNHANDLE h = 0;
    // Measured on stock dpnet: the game keeps sending while it is not yet
    // in a session; those sends must fail with NOCONNECTION and stay silent.
    DPN_BUFFER_DESC pre{}; pre.dwBufferSize = 4; pre.pBufferData = (BYTE*)"\x10\x00\x01\x00";
    DPNHANDLE ph = 0;
    Check(j1->SendTo(DPNID_ALL_PLAYERS_GROUP, &pre, 1, 0, NULL, &ph, DPNSEND_GUARANTEED) == DPNERR_NOCONNECTION,
          "SendTo before connect = NOCONNECTION");
    // EnumHosts answers at once; cancelling it completes the op like stock.
    DPNHANDLE eh = 0;
    Check(j1->EnumHosts(&app, a1, NULL, NULL, 0, 5, 0, 10000, (void*)0xE1, &eh, 0) == DPNSUCCESS_PENDING, "EnumHosts pending");
    Check(WaitUntil([&] { return r1.enumResponse; }, 3000), "ENUM_HOSTS_RESPONSE delivered");
    Check(j1->CancelAsyncOperation(eh, 0) == S_OK, "CancelAsyncOperation(enum) = S_OK");
    Check(WaitUntil([&] { return r1.asyncDone; }, 3000) && r1.asyncOp == eh && r1.asyncHr == DPNERR_USERCANCEL,
          "ASYNC_OP_COMPLETE(USERCANCEL) for the enum");
    Check(j1->CancelAsyncOperation(0, 0) == DPNERR_INVALIDHANDLE, "CancelAsyncOperation(0) = INVALIDHANDLE");
    Sleep(200);
    Check(r1.msgs.empty() && r1.sendCompletes == 0, "no loopback / SEND_COMPLETE from the failed send");
    hr = j1->Connect(&app, a1, NULL, NULL, NULL, NULL, 0, (void*)0x11, (void*)0xA1, &h, 0);
    Check(hr == DPNSUCCESS_PENDING, "Connect() is pending");
    Check(WaitUntil([&] { return r1.connectDone; }, 8000) && SUCCEEDED(r1.connectHr), "joiner 1 CONNECT_COMPLETE ok");
    Check(r1.localId == 2, "joiner 1 is player 2");
    Check(Created(r1, 1) && Created(r1, 2), "joiner 1 has players 1 and 2");
    Check(WaitUntil([&] { return Created(rh, 2); }, 3000), "host got CREATE_PLAYER 2");
    Check(rh.indicate == 1, "host got INDICATE_CONNECT once");

    // ---- joiner 2 ------------------------------------------------------------
    printf("\n== joiner 2 ==\n");
    hr = j2->Connect(&app, a1, NULL, NULL, NULL, NULL, 0, (void*)0x22, (void*)0xB2, &h, 0);
    Check(WaitUntil([&] { return r2.connectDone; }, 8000) && SUCCEEDED(r2.connectHr), "joiner 2 CONNECT_COMPLETE ok");
    Check(r2.localId == 3, "joiner 2 is player 3");
    Check(Created(r2, 1) && Created(r2, 2) && Created(r2, 3), "joiner 2 has players 1, 2, 3 (roster)");
    Check(WaitUntil([&] { return Created(rh, 3); }, 3000), "host got CREATE_PLAYER 3");
    Check(WaitUntil([&] { return Created(r1, 3); }, 3000), "joiner 1 got CREATE_PLAYER 3 (IN_JOINED from the host)");

    // ---- refused joiner (game says no in INDICATE_CONNECT) ------------------
    printf("\n== refused joiner ==\n");
    rh.reject = true;
    IDirectPlay8Peer* j3 = MakePeer(); j3->Initialize(&r3, Handler, 0); SetName(j3, L"Eve");
    hr = j3->Connect(&app, a1, NULL, NULL, NULL, NULL, 0, NULL, NULL, &h, 0);
    Check(WaitUntil([&] { return r3.connectDone; }, 8000), "refused joiner gets CONNECT_COMPLETE");
    Check(r3.connectHr == DPNERR_HOSTREJECTEDCONNECTION, "... with HOSTREJECTEDCONNECTION");
    Check(r3.replySize == 24 && r3.replyRejected == 1, "... and the host's 24 reply bytes (rejected = 1)");
    Check(WaitUntil([&] { return rh.returnBuffers == 1; }, 2000) && rh.returnedCtx == (void*)0x55,
          "host gets RETURN_BUFFER for its reply data");
    Sleep(500);
    Check(!Created(rh, 4) && !Created(r1, 4) && !Created(r2, 4), "refused joiner never becomes a player anywhere");
    j3->Close(0);
    rh.reject = false;

    // ---- member that never says hello (ghost) --------------------------------
    printf("\n== ghost ==\n");
    DWORD session = SessionOf(url);
    sockaddr_in ra; bool raOk = RelaySockAddr(relay, ra);
    SOCKET gs = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    sockaddr_in any{}; any.sin_family = AF_INET; bind(gs, (sockaddr*)&any, sizeof(any));
    DWORD tmo = 1000; setsockopt(gs, SOL_SOCKET, SO_RCVTIMEO, (char*)&tmo, sizeof(tmo));
    BYTE role = 2;
    RawSend(gs, ra, 1, session, 0, 0, &role, 1);
    BYTE rx[256]; int rn = recv(gs, (char*)rx, sizeof(rx), 0);
    WORD ghost = rn >= (int)sizeof(RlHdr) ? ((RlHdr*)rx)->src : 0;
    Check(raOk && ghost != 0, "raw client got a relay slot");
    BYTE data[] = { 3, 0, 0, 0, 0, 0, 'G', 'H' };   // IN_DATA unreliable, seq 0
    for (int i = 0; i < 3; ++i) { RawSend(gs, ra, 3, session, ghost, 0, data, sizeof(data)); Sleep(50); }
    Sleep(500);
    Check(!Created(rh, ghost) && !Created(r1, ghost) && !Created(r2, ghost), "data from a member nobody announced creates no player");
    DPNID ids[16]; DWORD nids = 16;
    Check(host->EnumPlayersAndGroups(ids, &nids, DPNENUM_PLAYERS) == S_OK && nids == 3, "host still has 3 players");
    RawSend(gs, ra, 4, session, ghost, 0, NULL, 0);
    closesocket(gs);

    // ---- info -------------------------------------------------------------------
    printf("\n== player info ==\n");
    BYTE pib[256]; DWORD pis = sizeof(pib);
    hr = host->GetPeerInfo(3, (DPN_PLAYER_INFO*)pib, &pis, 0);
    Check(hr == S_OK && wcscmp(((DPN_PLAYER_INFO*)pib)->pwszName, L"Bob") == 0, "host GetPeerInfo(3) = 'Bob'");
    pis = sizeof(pib);
    hr = j2->GetPeerInfo(1, (DPN_PLAYER_INFO*)pib, &pis, 0);
    Check(hr == S_OK && (((DPN_PLAYER_INFO*)pib)->dwPlayerFlags & DPNPLAYER_HOST), "joiner 2 GetPeerInfo(1) flags HOST");
    nids = 8;
    hr = j1->EnumPlayersAndGroups(ids, &nids, DPNENUM_PLAYERS);
    Check(hr == S_OK && nids == 3, "joiner 1 EnumPlayersAndGroups = 3 players");
    void* ctx = NULL;
    hr = host->GetPlayerContext(2, &ctx, 0);
    Check(hr == S_OK && ctx == (void*)(ULONG_PTR)0x1002, "host GetPlayerContext(2) = value set in CREATE_PLAYER");
    BYTE adb[512]; DWORD ads = sizeof(adb);
    hr = j1->GetApplicationDesc((DPN_APPLICATION_DESC*)adb, &ads, 0);
    Check(hr == S_OK && ((DPN_APPLICATION_DESC*)adb)->dwCurrentPlayers == 3, "joiner 1 GetApplicationDesc: 3 current players");
    DPN_CONNECTION_INFO ci{}; ci.dwSize = sizeof(ci);
    Check(host->GetConnectionInfo(2, &ci, 0) == S_OK, "host GetConnectionInfo(2) ok");
    Check(host->GetConnectionInfo(9, &ci, 0) == DPNERR_INVALIDPLAYER, "host GetConnectionInfo(9) = INVALIDPLAYER");

    // ---- messaging ------------------------------------------------------------------
    printf("\n== messaging ==\n");
    Send(host, DPNID_ALL_PLAYERS_GROUP, "hi-all", 0);
    Check(WaitUntil([&] { return HasMsg(r1, 1, "hi-all") && HasMsg(r2, 1, "hi-all"); }, 3000), "broadcast reaches both joiners");
    Check(WaitUntil([&] { return HasMsg(rh, 1, "hi-all"); }, 1000), "broadcast loops back to the host (no NOLOOPBACK)");
    Send(host, DPNID_ALL_PLAYERS_GROUP, "no-echo", DPNSEND_NOLOOPBACK);
    Check(WaitUntil([&] { return HasMsg(r1, 1, "no-echo") && HasMsg(r2, 1, "no-echo"); }, 3000), "NOLOOPBACK broadcast reaches joiners");
    Sleep(300);
    Check(!HasMsg(rh, 1, "no-echo"), "NOLOOPBACK broadcast does not loop back");
    Send(j2, 2, "just-alice", DPNSEND_GUARANTEED);
    Check(WaitUntil([&] { return HasMsg(r1, 3, "just-alice"); }, 3000), "directed send 3 -> 2 arrives at Alice");
    Sleep(300);
    Check(!HasMsg(rh, 3, "just-alice") && !HasMsg(r2, 3, "just-alice"), "directed send stays private");
    Send(j1, 1, "to-host", DPNSEND_GUARANTEED);
    Check(WaitUntil([&] { return HasMsg(rh, 2, "to-host"); }, 3000), "directed send 2 -> 1 arrives at host");
    int scBefore = r1.sendCompletes;
    Send(j1, 2, "self-quiet", DPNSEND_NOCOMPLETE | DPNSEND_NONSEQUENTIAL);
    Check(WaitUntil([&] { return HasMsg(r1, 2, "self-quiet"); }, 2000), "send to own dpnid loops back");
    Sleep(200);
    Check(r1.sendCompletes == scBefore, "NOCOMPLETE send produces no SEND_COMPLETE");
    Send(j1, 2, "self-loud", DPNSEND_NONSEQUENTIAL);
    Check(WaitUntil([&] { return r1.sendCompletes == scBefore + 1; }, 2000), "normal send produces SEND_COMPLETE");
    Check(host->SendTo(42, NULL, 0, 0, NULL, NULL, DPNSEND_SYNC) == DPNERR_INVALIDPLAYER, "SendTo unknown player = INVALIDPLAYER");
    ci = DPN_CONNECTION_INFO{}; ci.dwSize = sizeof(ci);
    Check(host->GetConnectionInfo(2, &ci, 0) == S_OK && ci.dwRoundTripLatencyMS > 0, "round trip to Alice is measured (> 0 ms)");

    // ---- reliability under loss ----------------------------------------------------------
    printf("\n== reliability (30%% loss) ==\n");
    const int N = 50;
    bool rel = true;
    if (!g_setDrop) { printf("  (DpnTestSetDrop missing, skipped)\n"); }
    else {
        g_setDrop(30);
        for (int i = 0; i < N; ++i) { char t[16]; sprintf_s(t, "R%d", i); Send(host, DPNID_ALL_PLAYERS_GROUP, t, DPNSEND_GUARANTEED | DPNSEND_NOLOOPBACK); }
        WaitUntil([&] { return r1.relNext >= N && r2.relNext >= N; }, 15000);
        g_setDrop(0);
        rel = r1.relNext == N && r2.relNext == N && r1.relBad == 0 && r2.relBad == 0;
        printf("  alice %ld/%d (bad %ld), bob %ld/%d (bad %ld)\n", r1.relNext, N, r1.relBad, r2.relNext, N, r2.relBad);
        Check(rel, "50 guaranteed messages arrive exactly once, in order, at both joiners");
        DWORD qm = 0, qb = 0;
        Check(WaitUntil([&] { host->GetSendQueueInfo(DPNID_ALL_PLAYERS_GROUP, &qm, &qb, 0); return qm == 0; }, 3000), "send queue drains after acks");
    }

    // ---- NAT address change -------------------------------------------------------------
    printf("\n== NAT address change ==\n");
    if (!g_rebind) { printf("  (DpnTestRebind missing, skipped)\n"); }
    else {
        g_rebind(j2);
        Sleep(300);
        Send(j2, 1, "bob-moved", DPNSEND_GUARANTEED);
        Send(host, 3, "to-moved-bob", DPNSEND_GUARANTEED);
        Check(WaitUntil([&] { return HasMsg(rh, 3, "bob-moved"); }, 6000), "Bob's data reaches the host from his new port");
        Check(WaitUntil([&] { return HasMsg(r2, 1, "to-moved-bob"); }, 6000), "host's data reaches Bob at his new port");
        Check(!Destroyed(rh, 3) && !Destroyed(r1, 3) && !r2.terminated, "nobody dropped Bob");
    }

    // ---- kick -------------------------------------------------------------------------------
    printf("\n== kick ==\n");
    DWORD why = 0x100000;
    Check(j1->DestroyPeer(3, &why, 4, 0) == DPNERR_NOTHOST, "joiner DestroyPeer = NOTHOST");
    Check(host->DestroyPeer(3, &why, 4, 0) == S_OK, "host DestroyPeer(3, 0x100000)");
    Check(WaitUntil([&] { return r2.terminated; }, 5000), "Bob gets TERMINATE_SESSION");
    Check(r2.terminateHr == DPNERR_HOSTTERMINATEDSESSION, "... HOSTTERMINATEDSESSION");
    Check(r2.terminateData.size() == 4 && *(DWORD*)r2.terminateData.data() == 0x100000, "... with the host's 4 bytes (0x100000)");
    Check(WaitUntil([&] { return Destroyed(r2, 1) && Destroyed(r2, 3); }, 3000), "Bob gets DESTROY_PLAYER for the host and himself");
    Check(Destroyed(rh, 3) && rh.destroyReason[3] == DPNDESTROYPLAYERREASON_HOSTDESTROYEDPLAYER, "host: DESTROY_PLAYER 3 HOSTDESTROYEDPLAYER");
    Check(WaitUntil([&] { return Destroyed(r1, 3); }, 3000), "Alice: DESTROY_PLAYER 3");
    Check(!Destroyed(r1, 1) && !Destroyed(r1, 2) && !r1.terminated, "Alice stays in the session");
    Send(host, 2, "after-kick", DPNSEND_GUARANTEED);
    Check(WaitUntil([&] { return HasMsg(r1, 1, "after-kick"); }, 3000), "host and Alice keep talking");
    j2->Close(0);

    // ---- late joiner after a kick -------------------------------------------------------------
    printf("\n== late joiner ==\n");
    IDirectPlay8Peer* j4 = JoinSession(r4, L"Dave", url, app, NULL);
    Check(WaitUntil([&] { return r4.connectDone; }, 8000) && SUCCEEDED(r4.connectHr), "Dave CONNECT_COMPLETE ok");
    DPNID dave = r4.localId;
    Check(Created(r4, 1) && Created(r4, 2) && !Created(r4, 3), "Dave's roster: host and Alice, not the kicked Bob");
    Check(WaitUntil([&] { return Created(r1, dave); }, 3000), "Alice learns about Dave");
    Send(j4, 2, "dave-to-alice", DPNSEND_GUARANTEED);
    Check(WaitUntil([&] { return HasMsg(r1, dave, "dave-to-alice"); }, 3000), "Dave -> Alice directly");

    // ---- leaving ---------------------------------------------------------------------------
    printf("\n== leaving ==\n");
    j1->Close(0);
    Check(Destroyed(r1, 1) && Destroyed(r1, 2) && Destroyed(r1, dave), "closing joiner sees DESTROY_PLAYER for everyone");
    Check(r1.destroyCtx[1] == (void*)(ULONG_PTR)0x1001 && r1.destroyCtx[2] == (void*)(ULONG_PTR)0x1002,
          "DESTROY_PLAYER carries the player contexts");
    Check(WaitUntil([&] { return Destroyed(rh, 2) && Destroyed(r4, 2); }, 5000), "host and Dave get DESTROY_PLAYER 2");
    Check(rh.destroyReason[2] == DPNDESTROYPLAYERREASON_NORMAL, "reason NORMAL (said goodbye)");
    Check(rh.destroyCtx[2] == (void*)(ULONG_PTR)0x1002, "host's DESTROY_PLAYER 2 carries Alice's context");
    int daveDestroyedBefore = r4.destroyedBeforeTerminate;
    host->Close(0);
    Check(WaitUntil([&] { return r4.terminated; }, 5000), "Dave gets TERMINATE_SESSION when the host leaves");
    Check(r4.terminateHr == DPNERR_HOSTTERMINATEDSESSION, "TERMINATE_SESSION reports HOSTTERMINATEDSESSION");
    Check(WaitUntil([&] { return Destroyed(r4, 1) && Destroyed(r4, dave); }, 3000), "Dave gets DESTROY_PLAYER for the host and himself");
    Check(r4.destroyedBeforeTerminate == daveDestroyedBefore, "TERMINATE_SESSION came before those DESTROY_PLAYERs");
    Check(r4.destroyReason[1] == DPNDESTROYPLAYERREASON_SESSIONTERMINATED, "reason SESSIONTERMINATED");
    j4->Close(0);

    // ---- bogus session ----------------------------------------------------------------------
    printf("\n== bogus session ==\n");
    IDirectPlay8Peer* j5 = MakePeer(); j5->Initialize(&r5, Handler, 0); SetName(j5, L"Mallory");
    std::string bogus = url; size_t sp = bogus.find(";session=");
    if (sp != std::string::npos) bogus = bogus.substr(0, sp) + ";session=DEADBEEF";
    IDirectPlay8Address* a5 = MakeAddr(); a5->BuildFromURLA((char*)bogus.c_str());
    if (g_setJoinTimeout) g_setJoinTimeout(3000);
    hr = j5->Connect(&app, a5, NULL, NULL, NULL, NULL, 0, NULL, NULL, &h, 0);
    Check(WaitUntil([&] { return r5.connectDone; }, 6000) && FAILED(r5.connectHr), "connect to unknown session fails cleanly");
    j5->Close(0);

    for (DWORD i = 0; i < na; ++i) if (addrs[i]) addrs[i]->Release();
    a1->Release(); a5->Release();
    host->Release(); j1->Release(); j2->Release(); j3->Release(); j4->Release(); j5->Release();

    printf("\n=== RESULT: %s (%d failed) ===\n", g_fails == 0 ? "PASS" : "FAIL", g_fails);
    return g_fails == 0 ? 0 : 2;
}
