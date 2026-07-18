// dpnetreplace - a minimal DirectPlay8 replacement for Two Worlds 1.
//
// Implements the DirectPlay8 COM objects the game creates (Peer + Address)
// over its own logic instead of the Windows dpnet.dll, so multiplayer no
// longer depends on the Windows DirectPlay legacy feature. Registered
// per-user under WOW6432Node (admin-free), it is loaded by the 32-bit game
// via CoCreateInstance.
//
// Phase 3: real two-player UDP transport. The host binds a UDP socket and
// advertises its actual LAN address; a joiner parses that address and
// connects over UDP. The two replacement DLLs speak a tiny handshake
// (CONNECT/ACK) to agree on player ids, then relay every SendTo to the
// peer and deliver incoming packets as RECEIVE. Solo hosting still works
// via loopback (a lone host has no peer, so its sends loop back as before).
//
// The 12 methods the game calls are implemented (the solo-host 10 plus
// Connect + EnumHosts); the rest report DPNERR_UNSUPPORTED. Messages
// delivered: CREATE_PLAYER, DESTROY_PLAYER, RECEIVE, SEND_COMPLETE,
// CONNECT_COMPLETE, INDICATE_CONNECT.

#define WIN32_LEAN_AND_MEAN
#define INITGUID
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <dplay8.h>
#include <dpaddr.h>
#include <stdio.h>
#include <vector>
#include <map>
#include <deque>
#include <string>

#pragma comment(lib, "ws2_32.lib")

// Wire packets exchanged between two dpnetreplace instances.
#pragma pack(push, 1)
struct WirePkt { DWORD magic; BYTE type; };  // magic 'TWDP'
#pragma pack(pop)
static const DWORD WIRE_MAGIC = 0x50445754;  // 'TWDP' little-endian
enum { PKT_CONNECT = 1, PKT_ACK = 2, PKT_DATA = 3, PKT_BYE = 4 };

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
        // Always tear down the threads before freeing the state they touch,
        // even if the game Released without calling Close() (abort paths).
        // StopNet()/StopWorker() are idempotent, so a prior Close() is a no-op.
        StopNet();
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
        m_isHost = true;
        m_localDpnid = 1;
        // Bind a UDP socket on an ephemeral port; we advertise the actual
        // bound port via GetLocalHostAddresses so a joiner reaches us.
        OpenSocket(0);
        StartNet();
        Log("Peer::Host -> local player 1, udp port %u", m_boundPort);
        // The local player is created asynchronously on the worker thread.
        Job j; j.type = Job::CREATE_PLAYER; j.dpnid = 1;
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
        std::wstring ip = BestLanIpW();
        a->AddComponent(DPNA_KEY_HOSTNAME, ip.c_str(),
                        (DWORD)(ip.size() + 1) * sizeof(WCHAR), DPNA_DATATYPE_STRING);
        DWORD port = m_boundPort;
        a->AddComponent(DPNA_KEY_PORT, &port, sizeof(port), DPNA_DATATYPE_DWORD);
        prgpAddress[0] = a;
        *pcAddress = 1;
        Log("Peer::GetLocalHostAddresses -> %S:%u", ip.c_str(), m_boundPort);
        return S_OK;
    }

    STDMETHODIMP SendTo(DPNID dpnid, const DPN_BUFFER_DESC* prgBufferDesc, DWORD cBufferDesc,
                        DWORD, void* pvAsyncContext, DPNHANDLE* phAsyncHandle,
                        DWORD dwFlags) override {
        std::vector<BYTE> data;
        for (DWORD i = 0; i < cBufferDesc; ++i)
            data.insert(data.end(), prgBufferDesc[i].pBufferData,
                        prgBufferDesc[i].pBufferData + prgBufferDesc[i].dwBufferSize);

        bool async = (phAsyncHandle != NULL) && !(dwFlags & DPNSEND_SYNC);
        DPNHANDLE h = 0;
        if (async) { h = NextHandle(); if (phAsyncHandle) *phAsyncHandle = h; }

        // If a peer is connected and the target is the peer or the whole
        // session, ship the payload over UDP; it arrives there as RECEIVE.
        bool toPeer = m_hasPeer &&
                      (dpnid == m_remoteDpnid || dpnid == DPNID_ALL_PLAYERS_GROUP);
        if (toPeer) SendWire(PKT_DATA, m_localDpnid, data.data(), (DWORD)data.size());

        if (async) {
            Job sc; sc.type = Job::SEND_COMPLETE; sc.handle = h; sc.context = pvAsyncContext;
            Enqueue(sc);
        }
        // Loopback to self, preserving the solo behaviour the game relies on
        // (sender id = our own player, which the game recognises as itself).
        Job rc; rc.type = Job::RECEIVE; rc.dpnid = m_localDpnid; rc.data.swap(data);
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
        if (m_hasPeer) SendWire(PKT_BYE, m_localDpnid, NULL, 0);
        StopNet();
        if (m_localDpnid) { Job j; j.type = Job::DESTROY_PLAYER; j.dpnid = m_localDpnid; Enqueue(j); }
        m_hosting = false;
        StopWorker();
        // free any buffers the game never returned
        EnterCriticalSection(&m_lock);
        for (auto& kv : m_kept) free(kv.second);
        m_kept.clear();
        LeaveCriticalSection(&m_lock);
        return S_OK;
    }

    STDMETHODIMP Connect(const DPN_APPLICATION_DESC*, IDirectPlay8Address* pHostAddr,
                         IDirectPlay8Address*, const DPN_SECURITY_DESC*,
                         const DPN_SECURITY_CREDENTIALS*, const void*, DWORD,
                         void* pvPlayerContext, void* pvAsyncContext,
                         DPNHANDLE* phAsyncHandle, DWORD) override {
        m_playerContext = pvPlayerContext;
        m_isHost = false;
        std::string host; unsigned short port = 0;
        if (!ParseHostAddr(pHostAddr, host, port)) {
            Log("Peer::Connect -> could not parse host address");
            return DPNERR_INVALIDHOSTADDRESS;
        }
        OpenSocket(0);  // ephemeral local port
        m_peerAddr = {};
        m_peerAddr.sin_family = AF_INET;
        m_peerAddr.sin_port = htons(port);
        inet_pton(AF_INET, host.c_str(), &m_peerAddr.sin_addr);
        DPNHANDLE h = NextHandle();
        m_connectHandle = h; m_connectContext = pvAsyncContext;
        if (phAsyncHandle) *phAsyncHandle = h;
        StartNet();
        SendWire(PKT_CONNECT, 0, NULL, 0);   // knock on the host
        Log("Peer::Connect -> %s:%u", host.c_str(), port);
        return DPNSUCCESS_PENDING;
    }

    STDMETHODIMP EnumHosts(PDPN_APPLICATION_DESC, IDirectPlay8Address*, IDirectPlay8Address*,
                           PVOID, DWORD, DWORD, DWORD, DWORD, PVOID,
                           DPNHANDLE* pAsyncHandle, DWORD) override {
        // Two Worlds gets the exact host address from the lobby and connects
        // directly, so enumeration is a no-op that just reports pending.
        Log("Peer::EnumHosts (no-op)");
        if (pAsyncHandle) *pAsyncHandle = NextHandle();
        return DPNSUCCESS_PENDING;
    }

#include "peer_stubs.inc"

private:
    struct Job {
        enum Type { CREATE_PLAYER, DESTROY_PLAYER, RECEIVE, SEND_COMPLETE,
                    CONNECT_COMPLETE, INDICATE_CONNECT } type;
        DPNID dpnid = 0;          // player id (CREATE/DESTROY) or sender (RECEIVE)
        std::vector<BYTE> data;   // RECEIVE payload
        DPNHANDLE handle = 0;     // SEND_COMPLETE / CONNECT async handle
        void* context = NULL;     // async user context
        HRESULT result = S_OK;    // CONNECT_COMPLETE result
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

    // -- networking (Phase 3) -----------------------------------------

    void OpenSocket(unsigned short port) {
        m_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (m_sock == INVALID_SOCKET) { Log("socket() failed %d", WSAGetLastError()); return; }
        sockaddr_in a{}; a.sin_family = AF_INET; a.sin_addr.s_addr = INADDR_ANY;
        a.sin_port = htons(port);
        if (bind(m_sock, (sockaddr*)&a, sizeof(a)) != 0)
            Log("bind() failed %d", WSAGetLastError());
        sockaddr_in bound{}; int bl = sizeof(bound);
        if (getsockname(m_sock, (sockaddr*)&bound, &bl) == 0)
            m_boundPort = ntohs(bound.sin_port);
        // short recv timeout so the net loop can check the stop flag
        DWORD tmo = 200; setsockopt(m_sock, SOL_SOCKET, SO_RCVTIMEO, (char*)&tmo, sizeof(tmo));
    }

    void SendWire(BYTE type, DPNID sender, const void* payload, DWORD len) {
        if (m_sock == INVALID_SOCKET || !m_hasPeer && type == PKT_DATA) return;
        std::vector<BYTE> pkt(sizeof(WirePkt) + sizeof(DPNID) + len);
        WirePkt* h = (WirePkt*)pkt.data(); h->magic = WIRE_MAGIC; h->type = type;
        memcpy(pkt.data() + sizeof(WirePkt), &sender, sizeof(DPNID));
        if (len) memcpy(pkt.data() + sizeof(WirePkt) + sizeof(DPNID), payload, len);
        sendto(m_sock, (char*)pkt.data(), (int)pkt.size(), 0,
               (sockaddr*)&m_peerAddr, sizeof(m_peerAddr));
    }

    void StartNet() {
        m_netStop = false;
        m_netThread = CreateThread(NULL, 0, &ReplPeer::NetEntry, this, 0, NULL);
    }
    void StopNet() {
        if (m_netThread) {
            m_netStop = true;
            WaitForSingleObject(m_netThread, INFINITE);
            CloseHandle(m_netThread); m_netThread = NULL;
        }
        if (m_sock != INVALID_SOCKET) { closesocket(m_sock); m_sock = INVALID_SOCKET; }
    }
    static DWORD WINAPI NetEntry(LPVOID p) { ((ReplPeer*)p)->NetLoop(); return 0; }

    void NetLoop() {
        std::vector<BYTE> buf(65536);
        while (!m_netStop) {
            sockaddr_in from{}; int fl = sizeof(from);
            int n = recvfrom(m_sock, (char*)buf.data(), (int)buf.size(), 0,
                             (sockaddr*)&from, &fl);
            if (n < (int)(sizeof(WirePkt) + sizeof(DPNID))) continue;
            WirePkt* h = (WirePkt*)buf.data();
            if (h->magic != WIRE_MAGIC) continue;
            DPNID sender; memcpy(&sender, buf.data() + sizeof(WirePkt), sizeof(DPNID));
            BYTE* body = buf.data() + sizeof(WirePkt) + sizeof(DPNID);
            DWORD bodyLen = n - sizeof(WirePkt) - sizeof(DPNID);
            HandlePacket(h->type, sender, body, bodyLen, from);
        }
    }

    void HandlePacket(BYTE type, DPNID sender, BYTE* body, DWORD len, sockaddr_in& from) {
        switch (type) {
        case PKT_CONNECT: {          // host side: a joiner knocked
            if (!m_isHost || m_hasPeer) break;
            m_peerAddr = from;
            m_remoteDpnid = 2;
            m_hasPeer = true;
            Log("net: joiner connected, dpnid 2");
            SendWire(PKT_ACK, m_localDpnid, NULL, 0);   // reply carries no body; ids implied
            Job ic; ic.type = Job::INDICATE_CONNECT; Enqueue(ic);
            Job cp; cp.type = Job::CREATE_PLAYER; cp.dpnid = 2; Enqueue(cp);
            break;
        }
        case PKT_ACK: {              // joiner side: host accepted
            if (m_isHost || m_hasPeer) break;
            m_localDpnid = 2; m_remoteDpnid = 1; m_hasPeer = true;
            Log("net: host accepted, local dpnid 2, host dpnid 1");
            Job cc; cc.type = Job::CONNECT_COMPLETE; cc.handle = m_connectHandle;
            cc.context = m_connectContext; cc.result = S_OK; Enqueue(cc);
            Job hp; hp.type = Job::CREATE_PLAYER; hp.dpnid = 1; Enqueue(hp);  // host
            Job sp; sp.type = Job::CREATE_PLAYER; sp.dpnid = 2; Enqueue(sp);  // self
            break;
        }
        case PKT_DATA: {
            Job rc; rc.type = Job::RECEIVE; rc.dpnid = sender;
            rc.data.assign(body, body + len); Enqueue(rc);
            break;
        }
        case PKT_BYE: {
            if (m_hasPeer) {
                Job dp; dp.type = Job::DESTROY_PLAYER; dp.dpnid = m_remoteDpnid; Enqueue(dp);
                m_hasPeer = false;
            }
            break;
        }
        }
    }

    static bool ParseHostAddr(IDirectPlay8Address* a, std::string& host, unsigned short& port) {
        if (!a) return false;
        WCHAR name[256]; DWORD sz = sizeof(name); DWORD type = 0;
        if (a->GetComponentByName(DPNA_KEY_HOSTNAME, name, &sz, &type) != S_OK) return false;
        char h[256]; WideCharToMultiByte(CP_ACP, 0, name, -1, h, sizeof(h), NULL, NULL);
        host = h;
        DWORD p = 0; sz = sizeof(p); type = 0;
        if (a->GetComponentByName(DPNA_KEY_PORT, &p, &sz, &type) == S_OK) port = (unsigned short)p;
        return !host.empty() && port != 0;
    }

    static std::wstring BestLanIpW() {
        // Prefer a private-range IPv4 the local host resolves to.
        std::wstring best = L"127.0.0.1";
        char hn[256];
        if (gethostname(hn, sizeof(hn)) != 0) return best;
        addrinfo hints{}; hints.ai_family = AF_INET; addrinfo* res = NULL;
        if (getaddrinfo(hn, NULL, &hints, &res) != 0) return best;
        for (addrinfo* p = res; p; p = p->ai_next) {
            sockaddr_in* s = (sockaddr_in*)p->ai_addr;
            DWORD ip = ntohl(s->sin_addr.s_addr);
            BYTE b1 = (ip >> 24) & 0xFF, b2 = (ip >> 16) & 0xFF;
            bool priv = (b1 == 192 && b2 == 168) || (b1 == 10) ||
                        (b1 == 172 && b2 >= 16 && b2 <= 31);
            if (priv) {
                char buf[64]; inet_ntop(AF_INET, &s->sin_addr, buf, sizeof(buf));
                WCHAR w[64]; MultiByteToWideChar(CP_ACP, 0, buf, -1, w, 64);
                best = w; break;
            }
        }
        freeaddrinfo(res);
        return best;
    }

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
        case Job::INDICATE_CONNECT: {
            // Host is asked to accept an incoming connection. Returning
            // S_OK from the handler accepts it.
            DPNMSG_INDICATE_CONNECT m{}; m.dwSize = sizeof(m);
            m_handler(m_context, DPN_MSGID_INDICATE_CONNECT, &m);
            break;
        }
        case Job::CREATE_PLAYER: {
            DPNMSG_CREATE_PLAYER m{}; m.dwSize = sizeof(m);
            m.dpnidPlayer = j.dpnid;
            m.pvPlayerContext = (j.dpnid == m_localDpnid) ? m_playerContext : NULL;
            m_handler(m_context, DPN_MSGID_CREATE_PLAYER, &m);
            break;
        }
        case Job::DESTROY_PLAYER: {
            DPNMSG_DESTROY_PLAYER m{}; m.dwSize = sizeof(m);
            m.dpnidPlayer = j.dpnid;
            m.pvPlayerContext = (j.dpnid == m_localDpnid) ? m_playerContext : NULL;
            m.dwReason = DPNDESTROYPLAYERREASON_NORMAL;
            m_handler(m_context, DPN_MSGID_DESTROY_PLAYER, &m);
            break;
        }
        case Job::CONNECT_COMPLETE: {
            DPNMSG_CONNECT_COMPLETE m{}; m.dwSize = sizeof(m);
            m.hAsyncOp = j.handle; m.pvUserContext = j.context;
            m.hResultCode = j.result; m.dpnidLocal = m_localDpnid;
            m_handler(m_context, DPN_MSGID_CONNECT_COMPLETE, &m);
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
            m.dpnidSender = j.dpnid ? j.dpnid : m_localDpnid;
            m.pvPlayerContext = NULL;
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

    // networking (Phase 3)
    SOCKET m_sock = INVALID_SOCKET;
    HANDLE m_netThread = NULL;
    volatile bool m_netStop = false;
    bool m_isHost = false;
    DPNID m_localDpnid = 0;
    DPNID m_remoteDpnid = 0;
    bool m_hasPeer = false;
    sockaddr_in m_peerAddr{};
    DPNHANDLE m_connectHandle = 0;
    void* m_connectContext = NULL;
    unsigned short m_boundPort = 0;
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
        WSADATA wsa; WSAStartup(MAKEWORD(2, 2), &wsa);
        Log("=== dpnetreplace geladen (pid %lu) ===", GetCurrentProcessId());
    }
    return TRUE;
}
