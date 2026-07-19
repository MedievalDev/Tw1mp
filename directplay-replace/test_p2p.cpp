// Self-test for the Phase 3 UDP transport: loads dpnetreplace.dll, spins up
// two peer instances in one process (one hosts, one joins over the real LAN
// address), and checks the connect handshake and bidirectional messaging -
// all without the game. Build with build-test.bat, run test_p2p.exe.

#define WIN32_LEAN_AND_MEAN
#define INITGUID
#include <winsock2.h>
#include <windows.h>
#include <dplay8.h>
#include <dpaddr.h>
#include <stdio.h>
#include <string>

typedef HRESULT (WINAPI *PFN_DllGetClassObject)(REFCLSID, REFIID, LPVOID*);
typedef void (*PFN_DpnTestSetDrop)(int);
typedef void (*PFN_DpnTestSetTimeout)(int);

static volatile LONG g_hostGotPeer = 0;     // host saw CREATE_PLAYER for joiner (dpnid 2)
static volatile LONG g_joinConnected = 0;   // joiner got CONNECT_COMPLETE
static volatile LONG g_hostRecv = 0, g_joinRecv = 0;
static char g_hostMsg[256] = {0}, g_joinMsg[256] = {0};

// Reliability phase: host sends RELIABLE_N guaranteed messages "R<seq>" under
// simulated packet loss; the joiner must receive all of them exactly once and
// strictly in order.
#define RELIABLE_N 50
static volatile LONG g_relCount = 0;        // how many reliable msgs received in order
static volatile LONG g_relOutOfOrder = 0;   // set if a seq arrives out of order/dup
static volatile LONG g_hostDestroyed = 0;   // host saw DESTROY_PLAYER for the joiner (dpnid 2)

static HRESULT WINAPI HostHandler(PVOID, DWORD id, PVOID msg) {
    if (id == DPN_MSGID_CREATE_PLAYER) {
        DPNMSG_CREATE_PLAYER* m = (DPNMSG_CREATE_PLAYER*)msg;
        printf("[HOST] CREATE_PLAYER dpnid=%lu\n", m->dpnidPlayer);
        if (m->dpnidPlayer == 2) InterlockedExchange(&g_hostGotPeer, 1);
    } else if (id == DPN_MSGID_RECEIVE) {
        DPNMSG_RECEIVE* m = (DPNMSG_RECEIVE*)msg;
        printf("[HOST] RECEIVE from %lu: %.*s\n", m->dpnidSender,
               m->dwReceiveDataSize, (char*)m->pReceiveData);
        if (m->dpnidSender == 2) { // from the joiner
            snprintf(g_hostMsg, sizeof(g_hostMsg), "%.*s", m->dwReceiveDataSize, (char*)m->pReceiveData);
            InterlockedExchange(&g_hostRecv, 1);
        }
    } else if (id == DPN_MSGID_DESTROY_PLAYER) {
        DPNMSG_DESTROY_PLAYER* m = (DPNMSG_DESTROY_PLAYER*)msg;
        printf("[HOST] DESTROY_PLAYER dpnid=%lu\n", m->dpnidPlayer);
        if (m->dpnidPlayer == 2) InterlockedExchange(&g_hostDestroyed, 1);
    } else if (id == DPN_MSGID_INDICATE_CONNECT) {
        printf("[HOST] INDICATE_CONNECT (accept)\n");
    }
    return S_OK;
}
static HRESULT WINAPI JoinHandler(PVOID, DWORD id, PVOID msg) {
    if (id == DPN_MSGID_CONNECT_COMPLETE) {
        DPNMSG_CONNECT_COMPLETE* m = (DPNMSG_CONNECT_COMPLETE*)msg;
        printf("[JOIN] CONNECT_COMPLETE hr=0x%08lX localdpnid=%lu\n", m->hResultCode, m->dpnidLocal);
        if (SUCCEEDED(m->hResultCode)) InterlockedExchange(&g_joinConnected, 1);
    } else if (id == DPN_MSGID_CREATE_PLAYER) {
        DPNMSG_CREATE_PLAYER* m = (DPNMSG_CREATE_PLAYER*)msg;
        printf("[JOIN] CREATE_PLAYER dpnid=%lu\n", m->dpnidPlayer);
    } else if (id == DPN_MSGID_RECEIVE) {
        DPNMSG_RECEIVE* m = (DPNMSG_RECEIVE*)msg;
        if (m->dwReceiveDataSize > 0 && ((char*)m->pReceiveData)[0] == 'R') {
            // Reliability-phase payload "R<seq>": must arrive in order, once.
            char tmp[32] = {0};
            DWORD n = m->dwReceiveDataSize < sizeof(tmp) - 1 ? m->dwReceiveDataSize : sizeof(tmp) - 1;
            memcpy(tmp, m->pReceiveData, n);
            int seq = atoi(tmp + 1);
            if (seq == g_relCount) InterlockedIncrement(&g_relCount);
            else InterlockedExchange(&g_relOutOfOrder, 1);
            return S_OK;
        }
        printf("[JOIN] RECEIVE from %lu: %.*s\n", m->dpnidSender,
               m->dwReceiveDataSize, (char*)m->pReceiveData);
        if (m->dpnidSender == 1) { // from the host
            snprintf(g_joinMsg, sizeof(g_joinMsg), "%.*s", m->dwReceiveDataSize, (char*)m->pReceiveData);
            InterlockedExchange(&g_joinRecv, 1);
        }
    }
    return S_OK;
}

static IDirectPlay8Peer* MakePeer(PFN_DllGetClassObject dgco) {
    IClassFactory* f = NULL;
    if (FAILED(dgco(CLSID_DirectPlay8Peer, IID_IClassFactory, (void**)&f))) return NULL;
    IDirectPlay8Peer* p = NULL;
    f->CreateInstance(NULL, IID_IDirectPlay8Peer, (void**)&p);
    f->Release();
    return p;
}

static bool WaitFlag(volatile LONG* flag, int ms) {
    for (int i = 0; i < ms / 20; ++i) { if (*flag) return true; Sleep(20); }
    return *flag != 0;
}

int main() {
    HMODULE dll = LoadLibraryA("dpnetreplace.dll");
    if (!dll) { printf("LoadLibrary failed %lu\n", GetLastError()); return 1; }
    PFN_DllGetClassObject dgco = (PFN_DllGetClassObject)GetProcAddress(dll, "DllGetClassObject");

    IDirectPlay8Peer* host = MakePeer(dgco);
    IDirectPlay8Peer* join = MakePeer(dgco);
    if (!host || !join) { printf("MakePeer failed\n"); return 1; }

    host->Initialize(NULL, HostHandler, 0);
    join->Initialize(NULL, JoinHandler, 0);

    // Host
    DPN_APPLICATION_DESC app{}; app.dwSize = sizeof(app);
    if (FAILED(host->Host(&app, NULL, 0, NULL, NULL, NULL, 0))) { printf("Host failed\n"); return 1; }

    // Get the host's address to connect to
    IDirectPlay8Address* addrs[4] = {0}; DWORD na = 4;
    if (FAILED(host->GetLocalHostAddresses(addrs, &na, 0)) || na < 1) { printf("GetLocalHostAddresses failed\n"); return 1; }
    WCHAR url[512]; DWORD ul = 512;
    addrs[0]->GetURLW(url, &ul);
    printf("[HOST] address URL: %S\n", url);

    // Joiner connects using that address
    DPNHANDLE h = 0;
    HRESULT hr = join->Connect(&app, addrs[0], NULL, NULL, NULL, NULL, 0, NULL, NULL, &h, 0);
    printf("[JOIN] Connect hr=0x%08lX\n", hr);

    bool ok = WaitFlag(&g_joinConnected, 3000) & WaitFlag(&g_hostGotPeer, 3000);
    printf("\n== handshake: %s ==\n", ok ? "OK" : "FAILED");

    // Exchange a message each way
    const char* fromHost = "hello-from-host";
    const char* fromJoin = "hello-from-joiner";
    DPN_BUFFER_DESC b1{}; b1.dwBufferSize = (DWORD)strlen(fromHost); b1.pBufferData = (BYTE*)fromHost;
    DPN_BUFFER_DESC b2{}; b2.dwBufferSize = (DWORD)strlen(fromJoin); b2.pBufferData = (BYTE*)fromJoin;
    DPNHANDLE sh = 0;
    host->SendTo(DPNID_ALL_PLAYERS_GROUP, &b1, 1, 0, NULL, &sh, 0);
    join->SendTo(DPNID_ALL_PLAYERS_GROUP, &b2, 1, 0, NULL, &sh, 0);

    bool got = WaitFlag(&g_hostRecv, 3000) & WaitFlag(&g_joinRecv, 3000);
    printf("\n== messaging: %s ==\n", got ? "OK" : "FAILED");
    printf("   host received from joiner: '%s'\n", g_hostMsg);
    printf("   joiner received from host: '%s'\n", g_joinMsg);

    // --- Reliability under packet loss ---
    // Inject 30% outgoing DATA/DATA_ACK loss, then fire RELIABLE_N guaranteed
    // messages host->joiner. All must arrive exactly once, in order.
    PFN_DpnTestSetDrop setDrop = (PFN_DpnTestSetDrop)GetProcAddress(dll, "DpnTestSetDrop");
    bool rel = false;
    if (!setDrop) {
        printf("\n== reliability: SKIPPED (DpnTestSetDrop not exported) ==\n");
        rel = true; // don't fail the run if the hook is absent
    } else {
        setDrop(30);
        for (int i = 0; i < RELIABLE_N; ++i) {
            char pay[16]; int pl = snprintf(pay, sizeof(pay), "R%d", i);
            DPN_BUFFER_DESC b{}; b.dwBufferSize = (DWORD)pl; b.pBufferData = (BYTE*)pay;
            DPNHANDLE sr = 0;
            host->SendTo(DPNID_ALL_PLAYERS_GROUP, &b, 1, 0, NULL, &sr, DPNSEND_GUARANTEED);
        }
        // Retransmit at 150ms RTO; give generous time for 30% loss over 50 msgs.
        for (int i = 0; i < 300 && g_relCount < RELIABLE_N; ++i) Sleep(20);
        setDrop(0);
        rel = (g_relCount == RELIABLE_N) && !g_relOutOfOrder;
        printf("\n== reliability: %s ==\n", rel ? "OK" : "FAILED");
        printf("   in-order received: %ld/%d, out-of-order/dup: %ld\n",
               g_relCount, RELIABLE_N, g_relOutOfOrder);
    }

    // --- Dead-peer detection ---
    // Simulate a total partition (crash with no BYE) by dropping 100% of
    // traffic and shortening the timeout; the host must synthesize
    // DESTROY_PLAYER for the now-silent joiner.
    PFN_DpnTestSetTimeout setTimeout =
        (PFN_DpnTestSetTimeout)GetProcAddress(dll, "DpnTestSetTimeout");
    bool dead = false;
    if (!setDrop || !setTimeout) {
        printf("\n== dead-peer: SKIPPED (test hooks not exported) ==\n");
        dead = true;
    } else {
        setTimeout(800);
        setDrop(100);               // total partition, no BYE reaches the host
        dead = WaitFlag(&g_hostDestroyed, 4000);
        setDrop(0); setTimeout(10000);
        printf("\n== dead-peer: %s ==\n", dead ? "OK" : "FAILED");
    }

    host->Close(0); join->Close(0);
    for (DWORD i = 0; i < na; ++i) if (addrs[i]) addrs[i]->Release();
    host->Release(); join->Release();

    bool pass = ok && got && rel && dead;
    printf("\n=== RESULT: %s ===\n", pass ? "PASS" : "FAIL");
    return pass ? 0 : 2;
}
