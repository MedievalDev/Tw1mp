// dpnetrelay - DirectPlay8 replacement for Two Worlds 1 with a relay transport.
//
// Successor of directplay-replace/dpnetreplace.cpp. The COM shell (Peer +
// Address objects, compiler-generated vtables, message dispatch on a worker
// thread, RECEIVE buffer ownership) is the proven part of the old DLL and
// is kept. What is new is the transport underneath:
//
//   * No peer-to-peer. Every player, host included, opens ONE outbound UDP
//     socket to the TW1MP relay (tw1mp/relay.py). The relay hands the host
//     a session id, the host advertises "relay host + port + session" as
//     its DirectPlay address, joiners send to the same relay and the relay
//     forwards between the members. Nobody needs an inbound port, a
//     firewall rule, a VPN or a correct LAN address.
//   * N players. Member ids come from the relay (host = 1, joiners get the
//     next id, never reused) and double as DirectPlay player ids.
//   * Reliable/ordered delivery per peer pair for DPNSEND_GUARANTEED
//     (sequence numbers, acks, retransmit, reorder window), unreliable
//     sends go straight through. Loopback follows stock DirectPlay: a send
//     to the all-players group also reaches the sender unless
//     DPNSEND_NOLOOPBACK is set (verified against the Windows dpnet.dll by
//     directplay-capture/test_capture.cpp).
//   * Player bookkeeping the game may rely on with a second player:
//     GetPeerInfo, GetPlayerContext, EnumPlayersAndGroups,
//     GetApplicationDesc, GetConnectionInfo, GetSendQueueInfo, GetCaps.
//
// Wire format between DLL and relay: see tw1mp/relay.py (13-byte header
// 'TWRL' + type + session + src + dst). Inside RL_DATA the DLLs speak
// their own small protocol (IN_* below).
//
// Relay address (hosting side only; joiners take it from the session
// address): env TW1MP_RELAY, then HKCU\Software\TW1MP\Relay, then the
// "natresolver" component the game puts on the host device
// (EarthNet_AddNATResolverInHost), then EarthNet_NATResolverAddress, then
// the built-in community server. The official 2-worlds.com resolver is
// never taken as a relay. Registered per-user under WOW6432Node
// (register.ps1), no admin, loaded by the 32-bit game via CoCreateInstance.
//
// Session control the game relies on (see ReverseEngineering\TW1_Multiplayer
// \plan_directplay.md, 2.4):
//   * INDICATE_CONNECT is answered by the game before the joiner is let in;
//     a failure code rejects with the game's 24-byte reply data, which the
//     joiner gets in CONNECT_COMPLETE (HOSTREJECTEDCONNECTION).
//   * DestroyPeer(id, data, 4) kicks: the victim gets TERMINATE_SESSION with
//     the 4 bytes, everyone else DESTROY_PLAYER (HOSTDESTROYEDPLAYER).
//   * CREATE_PLAYER for the host itself is delivered inside Host().
//   * GetConnectionInfo reports a measured round trip (acks, relay echo).
//   * New members are announced by the host only; data from an id nobody
//     announced creates no player.
//   * A silent relay ends the session; a changed NAT address rebinds with
//     the token from HELLO_OK instead of dropping the player.

#define WIN32_LEAN_AND_MEAN
#define INITGUID
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <dplay8.h>
#include <dpaddr.h>
#include <stdio.h>
#include <share.h>
#include <vector>
#include <map>
#include <deque>
#include <string>

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "advapi32.lib")

// -- relay envelope (must match tw1mp/relay.py) ---------------------------

#pragma pack(push, 1)
struct RlHdr { DWORD magic; BYTE type; DWORD session; WORD src; WORD dst; };
#pragma pack(pop)
static const DWORD RL_MAGIC = 0x4C525754;  // 'TWRL' little-endian
enum { RL_HELLO = 1, RL_HELLO_OK = 2, RL_DATA = 3, RL_BYE = 4, RL_PING = 5,
       RL_ERROR = 6, RL_LEFT = 7 };
// ROLE_REBIND: [3][u32 token], src = our member id. Sent when the relay
// answers 'rebind' because our packets now come from a new address.
enum { ROLE_HOST = 1, ROLE_JOIN = 2, ROLE_REBIND = 3 };

// -- inner protocol between DLLs (payload of RL_DATA) ----------------------
//
//   IN_HELLO   [1][utf8 name]                        joiner -> everyone (only the host acts on it)
//   IN_WELCOME [2][u8 n]{[u16 id][u8 len][name]}*n   host -> joiner (roster)
//              [u32 flags][u32 max][u8 len][name]    session description
//              [u16 len][reply data]                 from the game's INDICATE_CONNECT
//   IN_DATA    [3][u8 kind][u32 seq]([u32 stamp])[payload]  any -> any (stamp only when reliable)
//   IN_ACK     [4][u32 seq][u32 stamp]                receiver -> sender (stamp echoed)
//   IN_BYE     [5]                                    leaving member -> all
//   IN_REJECT  [6][u32 hr][u16 len][reply data]       host -> refused joiner
//   IN_KICK    [7][u16 len][terminate data]           host -> kicked member
//   IN_JOINED  [8][u16 id][utf8 name]                 host -> members (reliable): new player
//   IN_DROPPED [9][u16 id]                            host -> members (reliable): kicked player
enum { IN_HELLO = 1, IN_WELCOME = 2, IN_DATA = 3, IN_ACK = 4, IN_BYE = 5,
       IN_REJECT = 6, IN_KICK = 7, IN_JOINED = 8, IN_DROPPED = 9 };

// -- tuning ------------------------------------------------------------------

static const DWORD RTO_MIN_MS = 100;            // retransmit timeout bounds (adaptive in between)
static const DWORD RTO_MAX_MS = 2000;
static const DWORD RTO_DEFAULT_MS = 150;        // until the first round trip is measured
static const DWORD RETRANSMIT_CAP_MS = 3000;    // backoff ceiling per packet
static DWORD g_peerLostMs = 15000;              // oldest unacked send this old -> peer lost (test-tunable)
static DWORD g_relaySilenceMs = 15000;          // no packet from an echoing relay this long -> session lost (test-tunable)
static const DWORD RECV_WINDOW = 256;           // reorder buffer bound per peer
static const DWORD RELAY_PING_MS = 2000;        // keepalive towards the relay
static const DWORD HELLO_RETRY_MS = 700;        // resend HELLO/IN_HELLO while unanswered
static const DWORD HOST_HELLO_TRIES = 4;        // Host() waits up to ~2.8s for the relay
static const DWORD KICK_RESEND_MS = 300;        // resend IN_KICK/IN_REJECT to a member that keeps talking
static const DWORD KICK_REPEAT = 3;             // IN_KICK copies sent at once
static DWORD g_joinTimeoutMs = 6000;            // Connect() gives up after this (test-tunable)
static const char* DEFAULT_RELAY = "87.106.168.34:17172";
static const unsigned short DEFAULT_RELAY_PORT = 17172;

// -- logging (next to the DLL, shared-readable, flushed per line) ------------

static CRITICAL_SECTION g_logLock;
static bool g_logReady = false;
static FILE* g_logFile = NULL;
static LONG g_objects = 0;
static HMODULE g_hModule = NULL;
static char g_logPath[MAX_PATH] = {0};
static int g_dropPct = 0;   // test hook: drop % of outgoing IN_DATA / IN_ACK
static bool DropRoll() { return g_dropPct > 0 && (rand() % 100) < g_dropPct; }

static void LogInit() {
    if (g_logReady) return;
    InitializeCriticalSection(&g_logLock);
    if (g_hModule && GetModuleFileNameA(g_hModule, g_logPath, MAX_PATH)) {
        char* slash = strrchr(g_logPath, '\\');
        if (slash) strcpy_s(slash + 1, MAX_PATH - (slash + 1 - g_logPath), "dpnetrelay.log");
    } else {
        strcpy_s(g_logPath, MAX_PATH, "dpnetrelay.log");
    }
    g_logFile = _fsopen(g_logPath, "a", _SH_DENYNO);
    g_logReady = true;
}
static void Log(const char* fmt, ...) {
    if (!g_logReady || !g_logFile) return;
    EnterCriticalSection(&g_logLock);
    SYSTEMTIME st; GetLocalTime(&st);
    // pid per line: two game instances on one PC share this log.
    fprintf(g_logFile, "[%02d:%02d:%02d.%03d p=%5lu t=%5lu] ", st.wHour, st.wMinute, st.wSecond,
            st.wMilliseconds, GetCurrentProcessId(), GetCurrentThreadId());
    va_list a; va_start(a, fmt); vfprintf(g_logFile, fmt, a); va_end(a);
    fprintf(g_logFile, "\n");
    fflush(g_logFile);
    LeaveCriticalSection(&g_logLock);
}

static std::string ToUtf8(const std::wstring& w) {
    if (w.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), NULL, 0, NULL, NULL);
    std::string s(n, 0);
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), &s[0], n, NULL, NULL);
    return s;
}
static std::wstring FromUtf8(const char* p, int len) {
    if (len <= 0) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, p, len, NULL, 0);
    std::wstring w(n, 0);
    MultiByteToWideChar(CP_UTF8, 0, p, len, &w[0], n);
    return w;
}
static std::wstring Widen(const std::string& s) { return FromUtf8(s.c_str(), (int)s.size()); }

// -- relay address lookup ------------------------------------------------------

struct RelayTarget { std::string host; unsigned short port = DEFAULT_RELAY_PORT; std::string source; };

static bool ParseHostPort(const std::string& in, std::string& host, unsigned short& port) {
    std::string s = in;
    while (!s.empty() && (s.back() == ' ' || s.back() == '\r' || s.back() == '\n')) s.pop_back();
    size_t colon = s.rfind(':');
    if (colon == std::string::npos) { host = s; port = DEFAULT_RELAY_PORT; }
    else { host = s.substr(0, colon); int p = atoi(s.c_str() + colon + 1); if (p <= 0 || p > 65535) return false; port = (unsigned short)p; }
    return !host.empty();
}

// A NAT resolver value is a relay candidate unless it is empty or points at
// the official servers (warnet.2-worlds.com etc.), which run no relay. A
// DirectPlay resolver list may hold several entries; the first one counts.
static bool UsableResolver(std::string& s) {
    size_t comma = s.find(',');
    if (comma != std::string::npos) s.resize(comma);
    while (!s.empty() && s[0] == ' ') s.erase(0, 1);
    if (s.empty()) return false;
    std::string low = s;
    for (auto& ch : low) ch = (char)tolower((unsigned char)ch);
    return low.find("2-worlds") == std::string::npos;
}

static RelayTarget ReadRelayConfig(const std::string& deviceResolver) {
    RelayTarget t;
    char buf[512] = {0};
    std::string dev = deviceResolver;
    DWORD sz = sizeof(buf);
    if (GetEnvironmentVariableA("TW1MP_RELAY", buf, sizeof(buf)) > 0) {
        t.source = "env TW1MP_RELAY";
    } else if (RegGetValueA(HKEY_CURRENT_USER, "Software\\TW1MP", "Relay", RRF_RT_REG_SZ, NULL, buf, &sz) == ERROR_SUCCESS && buf[0]) {
        t.source = "HKCU\\Software\\TW1MP\\Relay";
    } else if (UsableResolver(dev)) {
        strcpy_s(buf, dev.c_str());
        t.source = "device natresolver";
    } else {
        std::string reg;
        sz = sizeof(buf); buf[0] = 0;
        if (RegGetValueA(HKEY_CURRENT_USER, "SOFTWARE\\Reality Pump\\TwoWorlds\\Network", "EarthNet_NATResolverAddress",
                         RRF_RT_REG_SZ, NULL, buf, &sz) == ERROR_SUCCESS) reg = buf;
        if (UsableResolver(reg)) {
            strcpy_s(buf, reg.c_str());
            t.source = "EarthNet_NATResolverAddress";
        } else {
            strcpy_s(buf, DEFAULT_RELAY);
            t.source = "built-in default";
        }
    }
    if (!ParseHostPort(buf, t.host, t.port)) { t.host.clear(); }
    return t;
}

static bool ResolveIPv4(const std::string& host, unsigned short port, sockaddr_in& out) {
    addrinfo hints{}; hints.ai_family = AF_INET; hints.ai_socktype = SOCK_DGRAM;
    addrinfo* res = NULL;
    if (getaddrinfo(host.c_str(), NULL, &hints, &res) != 0 || !res) return false;
    out = *(sockaddr_in*)res->ai_addr;
    out.sin_family = AF_INET;
    out.sin_port = htons(port);
    freeaddrinfo(res);
    return true;
}

// ===================================================================
//  IDirectPlay8Address  (unchanged from the P2P replacement)
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
    STDMETHODIMP AddComponent(const WCHAR* pwszName, const void* lpvData,
                              DWORD dwDataSize, DWORD dwDataType) override {
        if (!pwszName || !lpvData) return DPNERR_INVALIDPARAM;
        Comp c; c.name = pwszName; c.type = dwDataType;
        if (dwDataType == DPNA_DATATYPE_DWORD) {
            c.dword = *reinterpret_cast<const DWORD*>(lpvData);
        } else if (dwDataType == DPNA_DATATYPE_STRING_ANSI) {
            c.type = DPNA_DATATYPE_STRING;
            std::string a(reinterpret_cast<const char*>(lpvData), dwDataSize);
            while (!a.empty() && a.back() == '\0') a.pop_back();
            c.str = Widen(a);
        } else {
            c.str.assign(reinterpret_cast<const WCHAR*>(lpvData), dwDataSize / sizeof(WCHAR));
            while (!c.str.empty() && c.str.back() == L'\0') c.str.pop_back();
        }
        for (auto& e : m_comps) if (e.name == c.name) { e = c; return S_OK; }
        m_comps.push_back(c);
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
        std::string a = ToUtf8(BuildURL());
        DWORD need = (DWORD)a.size() + 1;
        if (!pszURL || !pdwNumChars || *pdwNumChars < need) {
            if (pdwNumChars) *pdwNumChars = need; return DPNERR_BUFFERTOOSMALL;
        }
        strcpy_s(pszURL, *pdwNumChars, a.c_str());
        *pdwNumChars = need;
        Log("Addr::GetURLA -> %s", a.c_str());
        return S_OK;
    }
    STDMETHODIMP BuildFromURLW(WCHAR* pwszURL) override {
        Log("Addr::BuildFromURLW <- %S", pwszURL ? pwszURL : L"(null)");
        return ParseURL(pwszURL ? std::wstring(pwszURL) : std::wstring());
    }
    STDMETHODIMP BuildFromURLA(CHAR* pszURL) override {
        Log("Addr::BuildFromURLA <- %s", pszURL ? pszURL : "(null)");
        return ParseURL(pszURL ? Widen(pszURL) : std::wstring());
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
        if (!pdpaAddress) return DPNERR_INVALIDPARAM;
        WCHAR url[2048]; DWORD n = 2048;
        if (pdpaAddress->GetURLW(url, &n) == S_OK) return ParseURL(url);
        return S_OK;
    }
    STDMETHODIMP IsEqual(IDirectPlay8Address* pdpaAddress) override {
        if (!pdpaAddress) return DPNERR_INVALIDPARAM;
        WCHAR url[2048]; DWORD n = 2048;
        if (pdpaAddress->GetURLW(url, &n) != S_OK) return DPNSUCCESS_NOTEQUAL;
        return BuildURL() == url ? DPNSUCCESS_EQUAL : DPNSUCCESS_NOTEQUAL;
    }
    STDMETHODIMP Clear() override { m_comps.clear(); m_hasSP = m_hasDevice = false; return S_OK; }
    STDMETHODIMP GetUserData(void*, DWORD*) override { return DPNERR_DOESNOTEXIST; }
    STDMETHODIMP SetUserData(const void*, DWORD) override { return S_OK; }
    STDMETHODIMP BuildFromDirectPlay4Address(void*, DWORD) override { return DPNERR_UNSUPPORTED; }

    // Helpers for our own use.
    bool GetString(const WCHAR* name, std::wstring& out) {
        for (auto& c : m_comps) if (c.name == name && c.type != DPNA_DATATYPE_DWORD) { out = c.str; return true; }
        return false;
    }

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
            wchar_t g[80];
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
        size_t pos = 0;
        while (pos < body.size()) {
            size_t semi = body.find(L';', pos);
            std::wstring tok = body.substr(pos, semi == std::wstring::npos ? std::wstring::npos : semi - pos);
            size_t eq = tok.find(L'=');
            if (eq != std::wstring::npos) {
                std::wstring key = tok.substr(0, eq), val = tok.substr(eq + 1);
                if (key == L"provider") { m_sp = CLSID_DP8SP_TCPIP; m_hasSP = true; }
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

static const WCHAR* KEY_SESSION = L"session";
static const WCHAR* DEFAULT_SESSION_NAME = L"EarthNetSession";   // what Two Worlds names its sessions
static const DWORD DEFAULT_SESSION_FLAGS = 0x44;                 // MIGRATE_HOST | NODPNSVR, as the game hosts
static const DWORD DEFAULT_MAX_PLAYERS = 8;

// Every DLL derives the same instance GUID from the relay session id, so
// host and joiners agree on it without an extra round trip.
static GUID InstanceGuidFor(DWORD session) {
    GUID g = { session, 0x5457, 0x314D, { 'P', 'R', 'E', 'L', 'A', 'Y', 0, 0 } };
    return g;
}

static std::string HexPrefix(const BYTE* p, DWORD n, DWORD max = 16) {
    std::string s; char t[4];
    DWORD m = n < max ? n : max;
    for (DWORD i = 0; i < m; ++i) { sprintf_s(t, "%02X", p[i]); s += t; }
    if (n > max) s += "..";
    return s;
}

// ===================================================================
//  IDirectPlay8Peer over the relay
// ===================================================================

class RelayPeer : public IDirectPlay8Peer {
public:
    RelayPeer() : m_ref(1) {
        InterlockedIncrement(&g_objects);
        InitializeCriticalSection(&m_lock);
        InitializeCriticalSection(&m_memLock);
        m_helloEvt = CreateEventW(NULL, TRUE, FALSE, NULL);
    }
    ~RelayPeer() {
        StopNet();
        StopWorker();
        for (auto& kv : m_kept) free(kv.second);
        m_kept.clear();
        if (m_helloEvt) CloseHandle(m_helloEvt);
        DeleteCriticalSection(&m_memLock);
        DeleteCriticalSection(&m_lock);
        InterlockedDecrement(&g_objects);
    }

    // ---- IUnknown ----------------------------------------------------------

    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (IsEqualIID(riid, IID_IUnknown) || IsEqualIID(riid, IID_IDirectPlay8Peer)) {
            *ppv = static_cast<IDirectPlay8Peer*>(this); AddRef(); return S_OK;
        }
        *ppv = NULL; return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&m_ref); }
    STDMETHODIMP_(ULONG) Release() override {
        LONG r = InterlockedDecrement(&m_ref);
        if (r == 0) delete this;
        return r;
    }

    // ---- setup ---------------------------------------------------------------

    STDMETHODIMP Initialize(PVOID pvUserContext, PFNDPNMESSAGEHANDLER pfn, DWORD flags) override {
        m_handler = pfn; m_context = pvUserContext;
        StartWorker();
        Log("Peer::Initialize flags=0x%lX", flags);
        return S_OK;
    }

    STDMETHODIMP EnumServiceProviders(const GUID*, const GUID*, DPN_SERVICE_PROVIDER_INFO* pInfo,
                                      DWORD* pcbEnumData, DWORD* pcReturned, DWORD) override {
        static const wchar_t kName[] = L"DirectPlay8 TCP/IP Service Provider";
        DWORD need = sizeof(DPN_SERVICE_PROVIDER_INFO) + sizeof(kName);
        if (pcReturned) *pcReturned = 1;
        if (!pInfo || !pcbEnumData || *pcbEnumData < need) {
            if (pcbEnumData) *pcbEnumData = need;
            Log("Peer::EnumServiceProviders (size query)");
            return DPNERR_BUFFERTOOSMALL;
        }
        WCHAR* dst = reinterpret_cast<WCHAR*>(reinterpret_cast<BYTE*>(pInfo) + sizeof(DPN_SERVICE_PROVIDER_INFO));
        wcscpy_s(dst, sizeof(kName) / sizeof(WCHAR), kName);
        pInfo->dwFlags = 0; pInfo->guid = CLSID_DP8SP_TCPIP;
        pInfo->pwszName = dst; pInfo->pvReserved = NULL; pInfo->dwReserved = 0;
        *pcbEnumData = need;
        Log("Peer::EnumServiceProviders (fill)");
        return S_OK;
    }

    STDMETHODIMP GetSPCaps(const GUID*, DPN_SP_CAPS* pCaps, DWORD) override {
        if (!pCaps || pCaps->dwSize < sizeof(DPN_SP_CAPS)) return DPNERR_INVALIDPARAM;
        pCaps->dwFlags = 0; pCaps->dwNumThreads = 2;
        pCaps->dwDefaultEnumCount = 5; pCaps->dwDefaultEnumRetryInterval = 1000;
        pCaps->dwDefaultEnumTimeout = 1000; pCaps->dwMaxEnumPayloadSize = 983;
        pCaps->dwBuffersPerThread = 1; pCaps->dwSystemBufferSize = 0x10000;
        return S_OK;
    }
    STDMETHODIMP SetSPCaps(const GUID*, const DPN_SP_CAPS*, DWORD) override { return S_OK; }
    STDMETHODIMP GetCaps(DPN_CAPS* c, DWORD) override {
        if (!c || c->dwSize < sizeof(DPN_CAPS)) return DPNERR_INVALIDPARAM;
        c->dwFlags = 0; c->dwConnectTimeout = 200; c->dwConnectRetries = 14; c->dwTimeoutUntilKeepAlive = 25000;
        return S_OK;
    }
    STDMETHODIMP SetCaps(const DPN_CAPS*, DWORD) override { return S_OK; }

    STDMETHODIMP SetPeerInfo(const DPN_PLAYER_INFO* pInfo, PVOID, DPNHANDLE* ph, DWORD) override {
        if (pInfo && (pInfo->dwInfoFlags & DPNINFO_NAME) && pInfo->pwszName) m_playerName = pInfo->pwszName;
        if (ph) *ph = 0;
        Log("Peer::SetPeerInfo (%S)", m_playerName.c_str());
        return S_OK;
    }

    // ---- hosting ---------------------------------------------------------------

    STDMETHODIMP Host(const DPN_APPLICATION_DESC* desc, IDirectPlay8Address** devices, DWORD nDevices,
                      const DPN_SECURITY_DESC*, const DPN_SECURITY_CREDENTIALS*,
                      void* pvPlayerContext, DWORD) override {
        ResetSession();
        m_isHost = true; m_localId = 1; m_playerContext = pvPlayerContext;
        StoreAppDesc(desc, true);

        std::string resolver;
        if (devices && nDevices && devices[0]) {
            WCHAR nat[256] = {0}; DWORD sz = sizeof(nat), type = 0;
            if (devices[0]->GetComponentByName(L"natresolver", nat, &sz, &type) == S_OK && type != DPNA_DATATYPE_DWORD)
                resolver = ToUtf8(nat);
        }
        m_relay = ReadRelayConfig(resolver);
        // A configured name that does not resolve (DNS record missing, typo) falls back to the
        // built-in community relay instead of hosting solo.
        if (!m_relay.host.empty() && m_relay.source != "built-in default" &&
            !ResolveIPv4(m_relay.host, m_relay.port, m_relayAddr)) {
            Log("Peer::Host -> relay '%s' (%s) does not resolve, using the built-in %s",
                m_relay.host.c_str(), m_relay.source.c_str(), DEFAULT_RELAY);
            std::string src = m_relay.source + ", unresolved -> built-in default";
            if (!ParseHostPort(DEFAULT_RELAY, m_relay.host, m_relay.port)) m_relay.host.clear();
            m_relay.source = src;
        }
        if (m_relay.host.empty() || !ResolveIPv4(m_relay.host, m_relay.port, m_relayAddr)) {
            Log("Peer::Host -> relay '%s' (%s) unusable, hosting SOLO ONLY (nobody can join)",
                m_relay.host.c_str(), m_relay.source.c_str());
            m_state = ST_HOSTING; m_session = 0;
        } else if (!OpenSocket()) {
            Log("Peer::Host -> socket failed, hosting SOLO ONLY");
            m_state = ST_HOSTING; m_session = 0;
        } else {
            m_state = ST_HOST_HELLO;
            ResetEvent(m_helloEvt);
            StartNet();
            BYTE role = ROLE_HOST;
            for (DWORD i = 0; i < HOST_HELLO_TRIES && !m_session; ++i) {
                SendRelay(RL_HELLO, 0, 0, 0, &role, 1);
                WaitForSingleObject(m_helloEvt, HELLO_RETRY_MS);
            }
            m_state = ST_HOSTING;
            if (m_session) Log("Peer::Host -> relay %s:%u (%s) session %08lX, local player 1",
                               m_relay.host.c_str(), m_relay.port, m_relay.source.c_str(), m_session);
            else Log("Peer::Host -> relay %s:%u (%s) did not answer, hosting SOLO ONLY",
                     m_relay.host.c_str(), m_relay.port, m_relay.source.c_str());
        }
        if (m_session) m_appDesc.guidInstance = InstanceGuidFor(m_session);
        m_appDesc.dwCurrentPlayers = 1;
        Log("Peer::Host appdesc flags=0x%lX max=%lu name='%S'", m_appDesc.dwFlags, m_appDesc.dwMaxPlayers, m_appName.c_str());
        // Stock dpnet delivers the host's own CREATE_PLAYER on the calling
        // thread before Host() returns; the game sends right after Host()
        // and expects its slot to exist. No DLL lock is held here.
        Job j; j.type = Job::CREATE_PLAYER; j.id = m_localId; Dispatch(j);
        return S_OK;
    }

    STDMETHODIMP GetLocalHostAddresses(IDirectPlay8Address** prgpAddress, DWORD* pcAddress, DWORD) override {
        if (!pcAddress) return DPNERR_INVALIDPARAM;
        if (!prgpAddress || *pcAddress < 1) { *pcAddress = 1; return DPNERR_BUFFERTOOSMALL; }
        ReplAddress* a = new ReplAddress();
        a->SetSP(&CLSID_DP8SP_TCPIP);
        std::wstring host = Widen(m_relay.host.empty() ? std::string("0.0.0.0") : m_relay.host);
        a->AddComponent(DPNA_KEY_HOSTNAME, host.c_str(), (DWORD)(host.size() + 1) * sizeof(WCHAR), DPNA_DATATYPE_STRING);
        DWORD port = m_relay.port;
        a->AddComponent(DPNA_KEY_PORT, &port, sizeof(port), DPNA_DATATYPE_DWORD);
        wchar_t sess[16]; swprintf_s(sess, L"%08lX", m_session);
        a->AddComponent(KEY_SESSION, sess, (DWORD)(wcslen(sess) + 1) * sizeof(WCHAR), DPNA_DATATYPE_STRING);
        prgpAddress[0] = a;
        *pcAddress = 1;
        Log("Peer::GetLocalHostAddresses -> %S:%lu session %S", host.c_str(), port, sess);
        return S_OK;
    }

    // ---- joining -----------------------------------------------------------------

    STDMETHODIMP EnumHosts(PDPN_APPLICATION_DESC pAppDesc, IDirectPlay8Address* pHostAddr,
                           IDirectPlay8Address*, PVOID, DWORD, DWORD, DWORD, DWORD,
                           PVOID pvUserContext, DPNHANDLE* pAsyncHandle, DWORD) override {
        // Two Worlds enumerates the address it got from the lobby and only
        // calls Connect once an ENUM_HOSTS_RESPONSE arrives. The lobby
        // already vouches for the game, so answer right away; a dead
        // session makes the following Connect fail instead.
        DPNHANDLE h = NextHandle();
        if (pAsyncHandle) *pAsyncHandle = h;
        std::string host; unsigned short port = 0; DWORD session = 0;
        if (!ParseHostAddr(pHostAddr, host, port, session)) {
            Log("Peer::EnumHosts -> no usable host address");
            return DPNERR_INVALIDHOSTADDRESS;
        }
        Log("Peer::EnumHosts -> %s:%u session %08lX", host.c_str(), port, session);
        m_enumHandle = h; m_enumContext = pvUserContext;
        Job j; j.type = Job::ENUM_RESPONSE; j.handle = h; j.context = pvUserContext;
        j.enumHost = host; j.enumPort = port; j.enumSession = session;
        if (pAppDesc) { j.appDesc = *pAppDesc; j.appName = pAppDesc->pwszSessionName ? pAppDesc->pwszSessionName : L"";
                        j.appPass = pAppDesc->pwszPassword ? pAppDesc->pwszPassword : L""; j.hasAppDesc = true; }
        Enqueue(j);
        return DPNSUCCESS_PENDING;
    }

    STDMETHODIMP Connect(const DPN_APPLICATION_DESC* desc, IDirectPlay8Address* pHostAddr,
                         IDirectPlay8Address*, const DPN_SECURITY_DESC*, const DPN_SECURITY_CREDENTIALS*,
                         const void*, DWORD, void* pvPlayerContext, void* pvAsyncContext,
                         DPNHANDLE* phAsyncHandle, DWORD) override {
        ResetSession();
        m_isHost = false; m_playerContext = pvPlayerContext;
        StoreAppDesc(desc, false);
        std::string host; unsigned short port = 0; DWORD session = 0;
        if (!ParseHostAddr(pHostAddr, host, port, session)) {
            Log("Peer::Connect -> could not parse host address");
            return DPNERR_INVALIDHOSTADDRESS;
        }
        if (session == 0) {
            Log("Peer::Connect -> host at %s:%u has no relay session (host is solo-only)", host.c_str(), port);
            return DPNERR_INVALIDHOSTADDRESS;
        }
        m_relay.host = host; m_relay.port = port; m_relay.source = "session address";
        if (!ResolveIPv4(host, port, m_relayAddr)) {
            Log("Peer::Connect -> cannot resolve relay %s", host.c_str());
            return DPNERR_INVALIDHOSTADDRESS;
        }
        if (!OpenSocket()) return DPNERR_GENERIC;
        m_session = session;
        m_appDesc.guidInstance = InstanceGuidFor(session);
        DPNHANDLE h = NextHandle();
        m_connectHandle = h; m_connectContext = pvAsyncContext;
        if (phAsyncHandle) *phAsyncHandle = h;
        m_state = ST_JOIN_HELLO; m_stateSince = GetTickCount(); m_lastHelloMs = 0;
        StartNet();
        Log("Peer::Connect -> relay %s:%u session %08lX (appdesc flags=0x%lX max=%lu name='%S' playerctx=%p)",
            host.c_str(), port, session, m_appDesc.dwFlags, m_appDesc.dwMaxPlayers, m_appName.c_str(), pvPlayerContext);
        return DPNSUCCESS_PENDING;
    }

    STDMETHODIMP CancelAsyncOperation(DPNHANDLE h, DWORD flags) override {
        // Measured on the Windows dpnet.dll: cancelling the enum handle ends
        // the enumeration with ASYNC_OP_COMPLETE, an unknown handle (the
        // game passes 0 after giving up) is INVALIDHANDLE.
        if (h != 0 && h == m_enumHandle) {
            m_enumHandle = 0;
            Job a; a.type = Job::ASYNC_COMPLETE; a.handle = h; a.context = m_enumContext; a.result = DPNERR_USERCANCEL;
            Enqueue(a);
            Log("Peer::CancelAsyncOperation op=0x%lX (enum) -> cancelled", (DWORD)h);
            return S_OK;
        }
        if (h != 0 && h == m_connectHandle && (m_state == ST_JOIN_HELLO || m_state == ST_JOIN_WELCOME)) {
            FailConnect(DPNERR_USERCANCEL, "cancelled by the game");
            Log("Peer::CancelAsyncOperation op=0x%lX (connect) -> cancelled", (DWORD)h);
            return S_OK;
        }
        Log("Peer::CancelAsyncOperation op=0x%lX flags=0x%lX -> INVALIDHANDLE", (DWORD)h, flags);
        return DPNERR_INVALIDHANDLE;
    }

    // ---- data ----------------------------------------------------------------------

    STDMETHODIMP SendTo(DPNID dpnid, const DPN_BUFFER_DESC* bufs, DWORD nbufs, DWORD,
                        void* pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD flags) override {
        // Measured on the Windows dpnet.dll: sends before a session exists
        // (the game keeps sending while EnumHosts is pending) fail with
        // NOCONNECTION and produce nothing, not even a loopback.
        if (m_state != ST_HOSTING && m_state != ST_CONNECTED) {
            if (phAsyncHandle && !(flags & DPNSEND_SYNC)) *phAsyncHandle = NextHandle();
            if (TrafficVerbose()) Log("Peer::SendTo to=%lu flags=0x%lX -> NOCONNECTION (no session yet)", dpnid, flags);
            return DPNERR_NOCONNECTION;
        }
        std::vector<BYTE> data;
        for (DWORD i = 0; i < nbufs; ++i)
            data.insert(data.end(), bufs[i].pBufferData, bufs[i].pBufferData + bufs[i].dwBufferSize);
        if (TrafficVerbose())
            Log("Peer::SendTo to=%lu size=%u flags=0x%lX hex=%s", dpnid, (unsigned)data.size(), flags,
                HexPrefix(data.data(), (DWORD)data.size()).c_str());
        bool async = (phAsyncHandle != NULL) && !(flags & DPNSEND_SYNC);
        DPNHANDLE h = 0;
        if (async) { h = NextHandle(); *phAsyncHandle = h; }

        bool reliable = (flags & DPNSEND_GUARANTEED) != 0;
        bool loopback = false;
        std::vector<WORD> targets;
        bool toAll = false;
        EnterCriticalSection(&m_memLock);
        // Only players the game knows (CREATE_PLAYER delivered) are targets;
        // a joiner still waiting for INDICATE_CONNECT is not one yet.
        if (dpnid == DPNID_ALL_PLAYERS_GROUP) {
            toAll = true;
            loopback = !(flags & DPNSEND_NOLOOPBACK);
            for (auto& kv : m_members) if (kv.second.announced) targets.push_back(kv.first);
        } else if (dpnid == m_localId) {
            loopback = true;
        } else if (IsPlayer((WORD)dpnid)) {
            targets.push_back((WORD)dpnid);
        } else {
            LeaveCriticalSection(&m_memLock);
            Log("Peer::SendTo -> unknown player %lu", dpnid);
            return DPNERR_INVALIDPLAYER;
        }
        if (m_session && m_sock != INVALID_SOCKET) {
            if (reliable) {
                for (WORD id : targets) SendReliable(m_members[id], data);
            } else if (toAll && targets.size() > 1) {
                SendData(0, false, 0, data);        // one broadcast for everyone
            } else {
                for (WORD id : targets) SendData(id, false, 0, data);
            }
        }
        LeaveCriticalSection(&m_memLock);

        // NOCOMPLETE: the game asked not to be told (measured: still PENDING
        // with a handle, but no SEND_COMPLETE message).
        if (async && !(flags & DPNSEND_NOCOMPLETE)) {
            Job sc; sc.type = Job::SEND_COMPLETE; sc.handle = h; sc.context = pvAsyncContext; Enqueue(sc);
        }
        if (loopback) { Job rc; rc.type = Job::RECEIVE; rc.id = m_localId; rc.data.swap(data); Enqueue(rc); }
        return async ? DPNSUCCESS_PENDING : S_OK;
    }

    STDMETHODIMP ReturnBuffer(DPNHANDLE hBufferHandle, DWORD) override {
        EnterCriticalSection(&m_lock);
        auto it = m_kept.find(hBufferHandle);
        if (it != m_kept.end()) { free(it->second); m_kept.erase(it); }
        LeaveCriticalSection(&m_lock);
        return S_OK;
    }

    STDMETHODIMP GetSendQueueInfo(DPNID dpnid, DWORD* pdwNumMsgs, DWORD* pdwNumBytes, DWORD) override {
        DWORD msgs = 0, bytes = 0;
        EnterCriticalSection(&m_memLock);
        for (auto& kv : m_members) {
            if (dpnid != DPNID_ALL_PLAYERS_GROUP && dpnid != kv.first) continue;
            msgs += (DWORD)kv.second.unacked.size();
            for (auto& u : kv.second.unacked) bytes += (DWORD)u.second.pkt.size();
        }
        LeaveCriticalSection(&m_memLock);
        if (pdwNumMsgs) *pdwNumMsgs = msgs;
        if (pdwNumBytes) *pdwNumBytes = bytes;
        return S_OK;
    }

    // ---- player / session info ------------------------------------------------------

    STDMETHODIMP GetPeerInfo(DPNID dpnid, DPN_PLAYER_INFO* pInfo, DWORD* pdwSize, DWORD) override {
        bool self = (dpnid == 0 || dpnid == m_localId);
        std::wstring nm;
        if (self) nm = m_playerName;
        else {
            EnterCriticalSection(&m_memLock);
            auto it = m_members.find((WORD)dpnid);
            bool found = it != m_members.end() && it->second.announced;
            if (found) nm = it->second.name;
            LeaveCriticalSection(&m_memLock);
            if (!found) { Log("Peer::GetPeerInfo(%lu) -> unknown", dpnid); return DPNERR_INVALIDPLAYER; }
        }
        DWORD need = sizeof(DPN_PLAYER_INFO) + (DWORD)(nm.size() + 1) * sizeof(WCHAR);
        if (!pInfo || !pdwSize || *pdwSize < need) {
            if (pdwSize) *pdwSize = need;
            return DPNERR_BUFFERTOOSMALL;
        }
        WCHAR* dst = reinterpret_cast<WCHAR*>(reinterpret_cast<BYTE*>(pInfo) + sizeof(DPN_PLAYER_INFO));
        wcscpy_s(dst, nm.size() + 1, nm.c_str());
        pInfo->dwSize = sizeof(DPN_PLAYER_INFO);
        pInfo->dwInfoFlags = DPNINFO_NAME;
        pInfo->pwszName = dst;
        pInfo->pvData = NULL; pInfo->dwDataSize = 0;
        DWORD flags = 0;
        if (self) flags |= DPNPLAYER_LOCAL;
        if ((self ? m_localId : (DPNID)dpnid) == 1) flags |= DPNPLAYER_HOST;
        pInfo->dwPlayerFlags = flags;
        *pdwSize = need;
        Log("Peer::GetPeerInfo(%lu) -> '%S' flags=0x%lX", dpnid, nm.c_str(), flags);
        return S_OK;
    }

    STDMETHODIMP GetPlayerContext(DPNID dpnid, PVOID* ppv, DWORD) override {
        if (!ppv) return DPNERR_INVALIDPARAM;
        if (dpnid == m_localId) { *ppv = m_localCtx; return S_OK; }
        EnterCriticalSection(&m_memLock);
        auto it = m_members.find((WORD)dpnid);
        bool found = it != m_members.end() && it->second.announced;
        if (found) *ppv = it->second.ctx;
        LeaveCriticalSection(&m_memLock);
        if (!found) Log("Peer::GetPlayerContext(%lu) -> unknown player", dpnid);
        return found ? S_OK : DPNERR_INVALIDPLAYER;
    }

    STDMETHODIMP EnumPlayersAndGroups(DPNID* prgdpnid, DWORD* pcdpnid, DWORD flags) override {
        if (!pcdpnid) return DPNERR_INVALIDPARAM;
        std::vector<DPNID> ids;
        if (!(flags & DPNENUM_GROUPS) || (flags & DPNENUM_PLAYERS)) {
            if (m_localId) ids.push_back(m_localId);
            EnterCriticalSection(&m_memLock);
            for (auto& kv : m_members) if (kv.second.announced) ids.push_back(kv.first);
            LeaveCriticalSection(&m_memLock);
        }
        if (!prgdpnid || *pcdpnid < ids.size()) { *pcdpnid = (DWORD)ids.size(); return DPNERR_BUFFERTOOSMALL; }
        for (size_t i = 0; i < ids.size(); ++i) prgdpnid[i] = ids[i];
        *pcdpnid = (DWORD)ids.size();
        return S_OK;
    }

    STDMETHODIMP GetApplicationDesc(DPN_APPLICATION_DESC* buf, DWORD* pcb, DWORD) override {
        if (!pcb) return DPNERR_INVALIDPARAM;
        DWORD nameBytes = (DWORD)(m_appName.size() + 1) * sizeof(WCHAR);
        DWORD passBytes = m_appPass.empty() ? 0 : (DWORD)(m_appPass.size() + 1) * sizeof(WCHAR);
        DWORD need = sizeof(DPN_APPLICATION_DESC) + nameBytes + passBytes;
        if (!buf || *pcb < need) { *pcb = need; return DPNERR_BUFFERTOOSMALL; }
        *buf = m_appDesc;
        buf->dwSize = sizeof(DPN_APPLICATION_DESC);
        buf->dwCurrentPlayers = (m_localId ? 1 : 0) + PlayerCount();
        BYTE* p = reinterpret_cast<BYTE*>(buf) + sizeof(DPN_APPLICATION_DESC);
        memcpy(p, m_appName.c_str(), nameBytes); buf->pwszSessionName = (WCHAR*)p; p += nameBytes;
        if (passBytes) { memcpy(p, m_appPass.c_str(), passBytes); buf->pwszPassword = (WCHAR*)p; }
        else buf->pwszPassword = NULL;
        buf->pvReservedData = NULL; buf->dwReservedDataSize = 0;
        buf->pvApplicationReservedData = NULL; buf->dwApplicationReservedDataSize = 0;
        *pcb = need;
        Log("Peer::GetApplicationDesc -> flags=0x%lX max=%lu cur=%lu name='%S'", buf->dwFlags, buf->dwMaxPlayers,
            buf->dwCurrentPlayers, m_appName.c_str());
        return S_OK;
    }
    STDMETHODIMP SetApplicationDesc(const DPN_APPLICATION_DESC* d, DWORD) override {
        if (!m_isHost) return DPNERR_NOTHOST;
        StoreAppDesc(d, true);
        return S_OK;
    }

    STDMETHODIMP GetConnectionInfo(DPNID dpnid, DPN_CONNECTION_INFO* ci, DWORD) override {
        // The game derives its re-request delay from this (RTT x 0.8 / 3.0),
        // so it must be a real figure: the smoothed ack round trip of that
        // player, else twice our own round trip to the relay.
        if (!ci || ci->dwSize < sizeof(DPN_CONNECTION_INFO)) return DPNERR_INVALIDPARAM;
        DWORD rtt = 0;
        bool known = (dpnid == m_localId);
        if (!known) {
            EnterCriticalSection(&m_memLock);
            auto it = m_members.find((WORD)dpnid);
            known = it != m_members.end() && it->second.announced;
            if (known) rtt = RttOf(it->second);
            LeaveCriticalSection(&m_memLock);
        }
        if (!known) return DPNERR_INVALIDPLAYER;
        DWORD sz = ci->dwSize; memset(ci, 0, sz); ci->dwSize = sz;
        ci->dwRoundTripLatencyMS = rtt;
        return S_OK;
    }

    // ---- teardown ----------------------------------------------------------------------

    STDMETHODIMP Close(DWORD flags) override {
        Log("Peer::Close flags=0x%lX", flags);
        if (m_session && m_sock != INVALID_SOCKET && m_state != ST_CLOSED) {
            BYTE bye = IN_BYE;
            SendRelay(RL_DATA, m_session, m_localId, 0, &bye, 1);
            SendRelay(RL_BYE, m_session, m_localId, 0, NULL, 0);
        }
        m_state = ST_CLOSED;
        StopNet();
        DestroyEveryone(DPNDESTROYPLAYERREASON_NORMAL);
        m_localId = 0; m_session = 0;
        StopWorker();
        EnterCriticalSection(&m_lock);
        for (auto& kv : m_kept) free(kv.second);
        m_kept.clear();
        LeaveCriticalSection(&m_lock);
        return S_OK;
    }

    STDMETHODIMP TerminateSession(void*, DWORD, DWORD) override {
        if (!m_isHost) return DPNERR_NOTHOST;
        Log("Peer::TerminateSession");
        if (m_session && m_sock != INVALID_SOCKET) {
            BYTE bye = IN_BYE;
            SendRelay(RL_DATA, m_session, m_localId, 0, &bye, 1);
            SendRelay(RL_BYE, m_session, m_localId, 0, NULL, 0);
            m_session = 0;
        }
        return S_OK;
    }
    // Kick (the game: 0x100000 / 0x200000 as 4 bytes, or no data for a late
    // join it does not allow). The victim gets TERMINATE_SESSION with the
    // data, the host and the others DESTROY_PLAYER HOSTDESTROYEDPLAYER.
    STDMETHODIMP DestroyPeer(DPNID dpnid, const void* data, DWORD size, DWORD) override {
        if (!m_isHost) return DPNERR_NOTHOST;
        if (dpnid == m_localId) return DPNERR_INVALIDPLAYER;
        std::vector<BYTE> kick;
        kick.push_back(IN_KICK);
        WORD n = (WORD)(data && size ? (size > 0xFFFF ? 0xFFFF : size) : 0);
        kick.push_back((BYTE)(n & 0xFF)); kick.push_back((BYTE)(n >> 8));
        if (n) kick.insert(kick.end(), (const BYTE*)data, (const BYTE*)data + n);
        WORD id = (WORD)dpnid;
        bool known = false;
        std::vector<WORD> others;
        EnterCriticalSection(&m_memLock);
        auto it = m_members.find(id);
        known = it != m_members.end();
        if (known) {
            Kicked k; k.pkt = kick; k.lastSent = GetTickCount();
            m_kicked[id] = k;
            for (auto& kv : m_members) if (kv.first != id && kv.second.announced) others.push_back(kv.first);
        }
        LeaveCriticalSection(&m_memLock);
        if (!known) { Log("Peer::DestroyPeer(%lu) -> unknown player", dpnid); return DPNERR_INVALIDPLAYER; }
        Log("Peer::DestroyPeer(%lu) data=%lu byte(s) %s", dpnid, (DWORD)n, HexPrefix((const BYTE*)data, n).c_str());
        if (m_session && m_sock != INVALID_SOCKET)
            for (DWORD i = 0; i < KICK_REPEAT; ++i) SendInner(id, kick);
        RemoveMember(id, DPNDESTROYPLAYERREASON_HOSTDESTROYEDPLAYER, "kicked by the host");
        BYTE dropped[3] = { IN_DROPPED, (BYTE)(id & 0xFF), (BYTE)(id >> 8) };
        std::vector<BYTE> dp(dropped, dropped + 3);
        EnterCriticalSection(&m_memLock);
        for (WORD o : others) { auto m = m_members.find(o); if (m != m_members.end()) SendReliable(m->second, dp, true); }
        LeaveCriticalSection(&m_memLock);
        return S_OK;
    }
    STDMETHODIMP GetPeerAddress(DPNID dpnid, IDirectPlay8Address** pa, DWORD) override {
        // Everyone is reachable through the relay only; hand out that address.
        if (!pa) return DPNERR_INVALIDPARAM;
        ReplAddress* a = new ReplAddress();
        a->SetSP(&CLSID_DP8SP_TCPIP);
        std::wstring host = Widen(m_relay.host);
        a->AddComponent(DPNA_KEY_HOSTNAME, host.c_str(), (DWORD)(host.size() + 1) * sizeof(WCHAR), DPNA_DATATYPE_STRING);
        DWORD port = m_relay.port;
        a->AddComponent(DPNA_KEY_PORT, &port, sizeof(port), DPNA_DATATYPE_DWORD);
        *pa = a;
        Log("Peer::GetPeerAddress(%lu) -> relay", dpnid);
        return S_OK;
    }

    // ---- groups / lobby: not used by the game ---------------------------------------------
    STDMETHODIMP CreateGroup(const DPN_GROUP_INFO*, void*, void*, DPNHANDLE*, DWORD) override { Log("Peer::CreateGroup (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP DestroyGroup(DPNID, PVOID, DPNHANDLE*, DWORD) override { Log("Peer::DestroyGroup (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP AddPlayerToGroup(DPNID, DPNID, PVOID, DPNHANDLE*, DWORD) override { Log("Peer::AddPlayerToGroup (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP RemovePlayerFromGroup(DPNID, DPNID, PVOID, DPNHANDLE*, DWORD) override { Log("Peer::RemovePlayerFromGroup (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP SetGroupInfo(DPNID, DPN_GROUP_INFO*, PVOID, DPNHANDLE*, DWORD) override { Log("Peer::SetGroupInfo (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP GetGroupInfo(DPNID, DPN_GROUP_INFO*, DWORD*, DWORD) override { Log("Peer::GetGroupInfo (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP EnumGroupMembers(DPNID, DPNID*, DWORD*, DWORD) override { Log("Peer::EnumGroupMembers (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP GetGroupContext(DPNID, PVOID*, DWORD) override { Log("Peer::GetGroupContext (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP RegisterLobby(DPNHANDLE, struct IDirectPlay8LobbiedApplication*, DWORD) override { Log("Peer::RegisterLobby (stub)"); return DPNERR_UNSUPPORTED; }

    void TestRebind() { m_testRebind = true; }   // self-test only

private:
    enum State { ST_IDLE, ST_HOST_HELLO, ST_HOSTING, ST_JOIN_HELLO, ST_JOIN_WELCOME, ST_CONNECTED, ST_CLOSED };

    struct Unacked { std::vector<BYTE> pkt; DWORD firstSend; DWORD lastSend; DWORD retries; };
    struct Member {
        WORD id = 0;
        std::wstring name;
        void* ctx = NULL;          // player context the game set in CREATE_PLAYER
        bool announced = false;    // CREATE_PLAYER delivered to the game
        bool indicating = false;   // host: INDICATE_CONNECT queued, game has not answered yet
        bool saidBye = false;
        std::vector<BYTE> reply;   // host: reply data the game gave in INDICATE_CONNECT (resent with WELCOME)
        DWORD seqOut = 0;
        std::map<DWORD, Unacked> unacked;
        DWORD seqIn = 0;
        std::map<DWORD, std::vector<BYTE>> reorder;   // [kind][payload]
        DWORD srtt = 0, rttvar = 0; bool hasRtt = false;
    };
    // Host: a member it refused or kicked. Talking to us again gets the same
    // answer (the first copy may have been lost), at most every KICK_RESEND_MS.
    struct Kicked { std::vector<BYTE> pkt; DWORD lastSent = 0; };

    struct Job {
        enum Type { CREATE_PLAYER, DESTROY_PLAYER, RECEIVE, SEND_COMPLETE, CONNECT_COMPLETE,
                    INDICATE_CONNECT, ENUM_RESPONSE, TERMINATE, ASYNC_COMPLETE } type;
        WORD id = 0;                 // player id (CREATE/DESTROY/INDICATE) or sender (RECEIVE)
        DWORD reason = 0;            // DESTROY_PLAYER reason
        void* playerCtx = NULL;      // DESTROY_PLAYER: context captured before the player was dropped
        std::vector<BYTE> data;      // RECEIVE payload, CONNECT_COMPLETE reply data, TERMINATE data
        DPNHANDLE handle = 0;        // async handle
        void* context = NULL;        // async user context
        HRESULT result = S_OK;       // CONNECT_COMPLETE / TERMINATE result
        std::string enumHost; unsigned short enumPort = 0; DWORD enumSession = 0;
        bool hasAppDesc = false; DPN_APPLICATION_DESC appDesc{}; std::wstring appName, appPass;
    };

    // Caller holds m_memLock (or reads a stable snapshot).
    DWORD RttOf(const Member& m) {
        if (m.hasRtt) return m.srtt ? m.srtt : 1;
        if (m_relayRttMs) return 2 * m_relayRttMs;
        return RTO_DEFAULT_MS;
    }
    DWORD RtoOf(const Member& m) {
        DWORD rto;
        if (m.hasRtt) rto = m.srtt + 4 * m.rttvar;
        else if (m_relayRttMs) rto = 2 * m_relayRttMs + 50;
        else rto = RTO_DEFAULT_MS;
        if (rto < RTO_MIN_MS) rto = RTO_MIN_MS;
        if (rto > RTO_MAX_MS) rto = RTO_MAX_MS;
        return rto;
    }
    bool IsPlayer(WORD id) {
        EnterCriticalSection(&m_memLock);
        auto it = m_members.find(id);
        bool ok = it != m_members.end() && it->second.announced;
        LeaveCriticalSection(&m_memLock);
        return ok;
    }
    DWORD PlayerCount() {
        DWORD n = 0;
        EnterCriticalSection(&m_memLock);
        for (auto& kv : m_members) if (kv.second.announced) ++n;
        LeaveCriticalSection(&m_memLock);
        return n;
    }

    // ---- worker thread (delivers messages to the game) ----------------------------------

    void StartWorker() {
        if (m_thread) return;
        m_stop = false;
        m_wake = CreateEventW(NULL, FALSE, FALSE, NULL);
        m_thread = CreateThread(NULL, 0, &RelayPeer::ThreadEntry, this, 0, NULL);
    }
    void StopWorker() {
        if (!m_thread) return;
        m_stop = true; if (m_wake) SetEvent(m_wake);
        // Close() may be called from inside the game's handler, i.e. ON the
        // worker thread; never join ourselves (the loop unwinds by itself).
        if (GetThreadId(m_thread) == GetCurrentThreadId()) return;
        WaitForSingleObject(m_thread, INFINITE);
        CloseHandle(m_thread); m_thread = NULL;
        EnterCriticalSection(&m_lock);
        HANDLE w = m_wake; m_wake = NULL;
        LeaveCriticalSection(&m_lock);
        if (w) CloseHandle(w);
    }
    void Enqueue(Job& j) {
        EnterCriticalSection(&m_lock);
        m_queue.push_back(std::move(j));
        if (m_wake) SetEvent(m_wake);
        LeaveCriticalSection(&m_lock);
    }
    DPNHANDLE NextHandle() { return (DPNHANDLE)InterlockedIncrement(&m_nextHandle); }
    static DWORD WINAPI ThreadEntry(LPVOID p) { ((RelayPeer*)p)->WorkerLoop(); return 0; }

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
            m.dpnidPlayer = j.id;
            // Remote: preset with what the game put into INDICATE_CONNECT (stock behaviour).
            m.pvPlayerContext = (j.id == m_localId) ? m_playerContext : CtxOf(j.id);
            m_handler(m_context, DPN_MSGID_CREATE_PLAYER, &m);
            if (j.id == m_localId) m_localCtx = m.pvPlayerContext;
            else {
                EnterCriticalSection(&m_memLock);
                auto it = m_members.find(j.id);
                if (it != m_members.end()) it->second.ctx = m.pvPlayerContext;
                LeaveCriticalSection(&m_memLock);
            }
            Log("-> CREATE_PLAYER %u (ctx %p)", j.id, m.pvPlayerContext);
            break;
        }
        case Job::DESTROY_PLAYER: {
            DPNMSG_DESTROY_PLAYER m{}; m.dwSize = sizeof(m);
            m.dpnidPlayer = j.id;
            m.pvPlayerContext = j.playerCtx;
            m.dwReason = j.reason ? j.reason : DPNDESTROYPLAYERREASON_NORMAL;
            Log("-> DESTROY_PLAYER %u reason %lu", j.id, m.dwReason);
            m_handler(m_context, DPN_MSGID_DESTROY_PLAYER, &m);
            break;
        }
        case Job::INDICATE_CONNECT: {
            ReplAddress* pa = new ReplAddress();
            pa->SetSP(&CLSID_DP8SP_TCPIP);
            std::wstring host = Widen(m_relay.host);
            pa->AddComponent(DPNA_KEY_HOSTNAME, host.c_str(), (DWORD)(host.size() + 1) * sizeof(WCHAR), DPNA_DATATYPE_STRING);
            DWORD port = m_relay.port;
            pa->AddComponent(DPNA_KEY_PORT, &port, sizeof(port), DPNA_DATATYPE_DWORD);
            DPNMSG_INDICATE_CONNECT m{}; m.dwSize = sizeof(m);
            m.pAddressPlayer = pa; m.pAddressDevice = pa;
            HRESULT hr = m_handler(m_context, DPN_MSGID_INDICATE_CONNECT, &m);
            std::vector<BYTE> reply;
            if (m.pvReplyData && m.dwReplyDataSize)
                reply.assign((BYTE*)m.pvReplyData, (BYTE*)m.pvReplyData + (m.dwReplyDataSize > 0xFFFF ? 0xFFFF : m.dwReplyDataSize));
            Log("-> INDICATE_CONNECT (player %u) game returned 0x%08lX, reply %u byte(s) %s", j.id, hr,
                (unsigned)reply.size(), HexPrefix(reply.data(), (DWORD)reply.size(), 24).c_str());
            pa->Release();
            if (hr == S_OK) AcceptJoiner(j.id, m.pvPlayerContext, reply);
            else RejectJoiner(j.id, DPNERR_HOSTREJECTEDCONNECTION, reply, "the game refused the connection");
            // The reply buffer belongs to the game; stock dpnet hands it back
            // once it has been sent (the game frees it by pvReplyContext).
            if (m.pvReplyData) {
                DPNMSG_RETURN_BUFFER rb{}; rb.dwSize = sizeof(rb);
                rb.hResultCode = S_OK; rb.pvBuffer = m.pvReplyData; rb.pvUserContext = m.pvReplyContext;
                Log("-> RETURN_BUFFER %p ctx %p", rb.pvBuffer, rb.pvUserContext);
                m_handler(m_context, DPN_MSGID_RETURN_BUFFER, &rb);
            }
            break;
        }
        case Job::CONNECT_COMPLETE: {
            DPNMSG_CONNECT_COMPLETE m{}; m.dwSize = sizeof(m);
            m.hAsyncOp = j.handle; m.pvUserContext = j.context;
            m.hResultCode = j.result; m.dpnidLocal = SUCCEEDED(j.result) ? m_localId : 0;
            if (!j.data.empty()) { m.pvApplicationReplyData = j.data.data(); m.dwApplicationReplyDataSize = (DWORD)j.data.size(); }
            Log("-> CONNECT_COMPLETE hr=0x%08lX local=%u reply %u byte(s) %s", j.result, m.dpnidLocal,
                (unsigned)j.data.size(), HexPrefix(j.data.data(), (DWORD)j.data.size(), 24).c_str());
            m_handler(m_context, DPN_MSGID_CONNECT_COMPLETE, &m);
            break;
        }
        case Job::TERMINATE: {
            DPNMSG_TERMINATE_SESSION m{}; m.dwSize = sizeof(m);
            m.hResultCode = j.result;
            if (!j.data.empty()) { m.pvTerminateData = j.data.data(); m.dwTerminateDataSize = (DWORD)j.data.size(); }
            Log("-> TERMINATE_SESSION hr=0x%08lX data %u byte(s) %s", j.result, (unsigned)j.data.size(),
                HexPrefix(j.data.data(), (DWORD)j.data.size()).c_str());
            m_handler(m_context, DPN_MSGID_TERMINATE_SESSION, &m);
            break;
        }
        case Job::ENUM_RESPONSE: {
            ReplAddress* sender = new ReplAddress();
            sender->SetSP(&CLSID_DP8SP_TCPIP);
            std::wstring hw = Widen(j.enumHost);
            sender->AddComponent(DPNA_KEY_HOSTNAME, hw.c_str(), (DWORD)(hw.size() + 1) * sizeof(WCHAR), DPNA_DATATYPE_STRING);
            DWORD p = j.enumPort;
            sender->AddComponent(DPNA_KEY_PORT, &p, sizeof(p), DPNA_DATATYPE_DWORD);
            wchar_t sess[16]; swprintf_s(sess, L"%08lX", j.enumSession);
            sender->AddComponent(KEY_SESSION, sess, (DWORD)(wcslen(sess) + 1) * sizeof(WCHAR), DPNA_DATATYPE_STRING);
            ReplAddress* device = new ReplAddress();
            device->SetSP(&CLSID_DP8SP_TCPIP);
            // Describe the session the way its host would: the game's own
            // enum desc has no name and max=0, a real response carries the
            // host's values. Instance GUID derives from the session id.
            DPN_APPLICATION_DESC desc = j.appDesc;
            std::wstring name = j.appName.empty() ? std::wstring(DEFAULT_SESSION_NAME) : j.appName;
            desc.dwSize = sizeof(desc);
            desc.pwszSessionName = &name[0];
            desc.pwszPassword = j.appPass.empty() ? NULL : &j.appPass[0];
            desc.pvReservedData = NULL; desc.dwReservedDataSize = 0;
            desc.pvApplicationReservedData = NULL; desc.dwApplicationReservedDataSize = 0;
            if (desc.dwMaxPlayers == 0) desc.dwMaxPlayers = DEFAULT_MAX_PLAYERS;
            if (desc.dwCurrentPlayers == 0) desc.dwCurrentPlayers = 1;
            if (desc.dwFlags == 0) desc.dwFlags = DEFAULT_SESSION_FLAGS;
            desc.guidInstance = InstanceGuidFor(j.enumSession);
            DPNMSG_ENUM_HOSTS_RESPONSE m{}; m.dwSize = sizeof(m);
            m.pAddressSender = sender; m.pAddressDevice = device;
            m.pApplicationDescription = &desc;
            m.pvResponseData = NULL; m.dwResponseDataSize = 0;
            m.pvUserContext = j.context; m.dwRoundTripLatencyMS = 1;
            Log("-> ENUM_HOSTS_RESPONSE %s:%u session %08lX", j.enumHost.c_str(), j.enumPort, j.enumSession);
            m_handler(m_context, DPN_MSGID_ENUM_HOSTS_RESPONSE, &m);
            sender->Release(); device->Release();
            break;
        }
        case Job::ASYNC_COMPLETE: {
            DPNMSG_ASYNC_OP_COMPLETE m{}; m.dwSize = sizeof(m);
            m.hAsyncOp = j.handle; m.pvUserContext = j.context; m.hResultCode = j.result;
            Log("-> ASYNC_OP_COMPLETE op=0x%lX hr=0x%08lX", (DWORD)j.handle, j.result);
            m_handler(m_context, DPN_MSGID_ASYNC_OP_COMPLETE, &m);
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
            if (!buf) break;
            if (size) memcpy(buf, j.data.data(), size);
            DPNHANDLE h = NextHandle();
            DPNMSG_RECEIVE m{}; m.dwSize = sizeof(m);
            m.dpnidSender = j.id ? j.id : m_localId;
            m.pvPlayerContext = (j.id == m_localId) ? m_localCtx : CtxOf(j.id);
            m.pReceiveData = buf; m.dwReceiveDataSize = size; m.hBufferHandle = h;
            bool verbose = TrafficVerbose();
            HRESULT hr = m_handler(m_context, DPN_MSGID_RECEIVE, &m);
            if (verbose) Log("-> RECEIVE from=%lu size=%lu hex=%s game=0x%08lX", m.dpnidSender, size,
                             HexPrefix(buf, size).c_str(), hr);
            if (hr == DPNSUCCESS_PENDING) {
                EnterCriticalSection(&m_lock);
                m_kept[h] = buf;
                LeaveCriticalSection(&m_lock);
            } else {
                free(buf);
            }
            break;
        }
        }
    }

    void* CtxOf(WORD id) {
        void* c = NULL;
        EnterCriticalSection(&m_memLock);
        auto it = m_members.find(id);
        if (it != m_members.end()) c = it->second.ctx;
        LeaveCriticalSection(&m_memLock);
        return c;
    }

    // ---- session state --------------------------------------------------------------------

    void ResetSession() {
        StopNet();
        EnterCriticalSection(&m_memLock);
        m_members.clear();
        m_kicked.clear();
        LeaveCriticalSection(&m_memLock);
        m_session = 0; m_localId = 0; m_localCtx = NULL; m_localDestroyed = false;
        m_state = ST_IDLE; m_lastRelayRx = 0; m_lastPingMs = 0; m_relayRttMs = 0; m_relayEchoes = 0;
        m_token = 0; m_hasToken = false; m_lastRebindMs = 0;
    }

    void StoreAppDesc(const DPN_APPLICATION_DESC* d, bool asHost) {
        if (d) {
            m_appDesc = *d;
            m_appName = d->pwszSessionName ? d->pwszSessionName : L"";
            m_appPass = d->pwszPassword ? d->pwszPassword : L"";
        } else {
            memset(&m_appDesc, 0, sizeof(m_appDesc)); m_appDesc.dwSize = sizeof(m_appDesc);
            m_appName.clear(); m_appPass.clear();
        }
        m_appDesc.dwSize = sizeof(m_appDesc);
        m_appDesc.pwszSessionName = NULL; m_appDesc.pwszPassword = NULL;
        m_appDesc.pvReservedData = NULL; m_appDesc.dwReservedDataSize = 0;
        m_appDesc.pvApplicationReservedData = NULL; m_appDesc.dwApplicationReservedDataSize = 0;
        // A joiner's own desc is sparse (the game passes name=NULL, max=0 to
        // EnumHosts/Connect). Fill in what the host would report; the real
        // values arrive with the WELCOME and overwrite these.
        if (!asHost) {
            if (m_appDesc.dwMaxPlayers == 0) m_appDesc.dwMaxPlayers = DEFAULT_MAX_PLAYERS;
            if (m_appDesc.dwFlags == 0) m_appDesc.dwFlags = DEFAULT_SESSION_FLAGS;
            if (m_appName.empty()) m_appName = DEFAULT_SESSION_NAME;
        }
    }

    static bool ParseHostAddr(IDirectPlay8Address* a, std::string& host, unsigned short& port, DWORD& session) {
        if (!a) return false;
        WCHAR name[256]; DWORD sz = sizeof(name); DWORD type = 0;
        if (a->GetComponentByName(DPNA_KEY_HOSTNAME, name, &sz, &type) != S_OK) return false;
        host = ToUtf8(name);
        DWORD p = 0; sz = sizeof(p); type = 0;
        if (a->GetComponentByName(DPNA_KEY_PORT, &p, &sz, &type) == S_OK) port = (unsigned short)p;
        WCHAR sess[64]; sz = sizeof(sess); type = 0;
        session = 0;
        if (a->GetComponentByName(KEY_SESSION, sess, &sz, &type) == S_OK) session = wcstoul(sess, NULL, 16);
        return !host.empty() && port != 0;
    }

    // ---- sockets / net thread ------------------------------------------------------------------

    bool OpenSocket() {
        m_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (m_sock == INVALID_SOCKET) { Log("socket() failed %d", WSAGetLastError()); return false; }
        sockaddr_in a{}; a.sin_family = AF_INET; a.sin_addr.s_addr = INADDR_ANY; a.sin_port = 0;
        if (bind(m_sock, (sockaddr*)&a, sizeof(a)) != 0) {
            Log("bind() failed %d", WSAGetLastError());
            closesocket(m_sock); m_sock = INVALID_SOCKET; return false;
        }
        DWORD tmo = 200; setsockopt(m_sock, SOL_SOCKET, SO_RCVTIMEO, (char*)&tmo, sizeof(tmo));
        return true;
    }
    void StartNet() {
        if (m_sock == INVALID_SOCKET || m_netThread) return;
        m_netStop = false;
        m_netThread = CreateThread(NULL, 0, &RelayPeer::NetEntry, this, 0, NULL);
    }
    void StopNet() {
        if (m_netThread) {
            m_netStop = true;
            if (GetThreadId(m_netThread) != GetCurrentThreadId()) {
                WaitForSingleObject(m_netThread, INFINITE);
                CloseHandle(m_netThread); m_netThread = NULL;
                if (m_sock != INVALID_SOCKET) { closesocket(m_sock); m_sock = INVALID_SOCKET; }
            }
            return;
        }
        if (m_sock != INVALID_SOCKET) { closesocket(m_sock); m_sock = INVALID_SOCKET; }
    }
    static DWORD WINAPI NetEntry(LPVOID p) { ((RelayPeer*)p)->NetLoop(); return 0; }

    void SendRelay(BYTE type, DWORD session, WORD src, WORD dst, const void* payload, DWORD len) {
        if (m_sock == INVALID_SOCKET) return;
        std::vector<BYTE> pkt(sizeof(RlHdr) + len);
        RlHdr* h = (RlHdr*)pkt.data();
        h->magic = RL_MAGIC; h->type = type; h->session = session; h->src = src; h->dst = dst;
        if (len) memcpy(pkt.data() + sizeof(RlHdr), payload, len);
        sendto(m_sock, (char*)pkt.data(), (int)pkt.size(), 0, (sockaddr*)&m_relayAddr, sizeof(m_relayAddr));
    }
    void SendInner(WORD dst, const std::vector<BYTE>& inner) {
        SendRelay(RL_DATA, m_session, m_localId, dst, inner.data(), (DWORD)inner.size());
    }

    // IN_DATA: [3][kind][seq u32][payload] for kind 0 = unreliable;
    //          [3][kind][seq u32][stamp u32][payload] for kind 1 = reliable
    // game data and kind 2 = reliable control message from the host
    // (IN_JOINED / IN_DROPPED, kept in order with the game data around it).
    // The stamp is the sender's tick at this (re)transmission and comes back
    // in the ack, so every ack gives a round trip, retransmits included.
    enum { KIND_UNRELIABLE = 0, KIND_RELIABLE = 1, KIND_CONTROL = 2 };
    static const DWORD STAMP_AT = 6;   // offset of the stamp in a reliable IN_DATA
    static std::vector<BYTE> BuildData(BYTE kind, DWORD seq, const std::vector<BYTE>& payload, DWORD stamp = 0) {
        DWORD head = 1 + 1 + sizeof(DWORD) + (kind != KIND_UNRELIABLE ? sizeof(DWORD) : 0);
        std::vector<BYTE> p(head + payload.size());
        p[0] = IN_DATA; p[1] = kind;
        memcpy(&p[2], &seq, sizeof(DWORD));
        if (kind != KIND_UNRELIABLE) memcpy(&p[STAMP_AT], &stamp, sizeof(DWORD));
        if (!payload.empty()) memcpy(&p[head], payload.data(), payload.size());
        return p;
    }
    void SendData(WORD dst, bool reliable, DWORD seq, const std::vector<BYTE>& payload) {
        if (DropRoll()) return;
        SendInner(dst, BuildData(reliable ? KIND_RELIABLE : KIND_UNRELIABLE, seq, payload, GetTickCount()));
    }
    // Caller holds m_memLock.
    void SendReliable(Member& m, const std::vector<BYTE>& payload, bool control = false) {
        DWORD seq = m.seqOut++;
        DWORD now = GetTickCount();
        Unacked u; u.pkt = BuildData(control ? KIND_CONTROL : KIND_RELIABLE, seq, payload, now);
        u.firstSend = now; u.lastSend = now; u.retries = 0;
        if (!DropRoll()) SendInner(m.id, u.pkt);
        m.unacked[seq] = std::move(u);
    }
    // IN_ACK: [4][seq u32][stamp u32 echoed from the data packet]
    void SendAck(WORD dst, DWORD seq, DWORD stamp) {
        if (DropRoll()) return;
        BYTE p[1 + 2 * sizeof(DWORD)]; p[0] = IN_ACK;
        memcpy(p + 1, &seq, sizeof(DWORD)); memcpy(p + 1 + sizeof(DWORD), &stamp, sizeof(DWORD));
        SendRelay(RL_DATA, m_session, m_localId, dst, p, sizeof(p));
    }
    void SendHello() {
        std::string nm = ToUtf8(m_playerName);
        std::vector<BYTE> p(1 + nm.size()); p[0] = IN_HELLO;
        if (!nm.empty()) memcpy(&p[1], nm.data(), nm.size());
        SendInner(0, p);
    }
    // Host -> joiner: every player in the session except the joiner, the
    // session description (flags, max players, name) and the reply data the
    // game gave in INDICATE_CONNECT.
    void SendWelcome(WORD to) {
        std::vector<BYTE> p; p.push_back(IN_WELCOME); p.push_back(0);
        BYTE n = 0;
        auto add = [&](WORD id, const std::wstring& name) {
            std::string nm = ToUtf8(name); if (nm.size() > 255) nm.resize(255);
            p.push_back((BYTE)(id & 0xFF)); p.push_back((BYTE)(id >> 8));
            p.push_back((BYTE)nm.size()); p.insert(p.end(), nm.begin(), nm.end()); ++n;
        };
        std::vector<BYTE> reply;
        EnterCriticalSection(&m_memLock);
        add(m_localId, m_playerName);
        for (auto& kv : m_members) if (kv.first != to && kv.second.announced) add(kv.first, kv.second.name);
        auto me = m_members.find(to);
        if (me != m_members.end()) reply = me->second.reply;
        LeaveCriticalSection(&m_memLock);
        p[1] = n;
        DWORD flags = m_appDesc.dwFlags, maxp = m_appDesc.dwMaxPlayers;
        p.insert(p.end(), (BYTE*)&flags, (BYTE*)&flags + 4);
        p.insert(p.end(), (BYTE*)&maxp, (BYTE*)&maxp + 4);
        std::string nm = ToUtf8(m_appName); if (nm.size() > 255) nm.resize(255);
        p.push_back((BYTE)nm.size()); p.insert(p.end(), nm.begin(), nm.end());
        WORD rl = (WORD)reply.size();
        p.push_back((BYTE)(rl & 0xFF)); p.push_back((BYTE)(rl >> 8));
        p.insert(p.end(), reply.begin(), reply.end());
        SendInner(to, p);
    }

    // Host, worker thread: the game accepted the joiner in INDICATE_CONNECT.
    // Welcome it, tell the others (reliably, ordered with the game data),
    // then CREATE_PLAYER on our side, in the same order as stock dpnet.
    void AcceptJoiner(WORD id, void* ctx, const std::vector<BYTE>& reply) {
        std::vector<WORD> others; std::wstring name;
        EnterCriticalSection(&m_memLock);
        auto it = m_members.find(id);
        bool ok = it != m_members.end() && it->second.indicating;
        if (ok) {
            Member& m = it->second;
            m.indicating = false; m.announced = true; m.ctx = ctx; m.reply = reply; name = m.name;
            for (auto& kv : m_members) if (kv.first != id && kv.second.announced) others.push_back(kv.first);
        }
        LeaveCriticalSection(&m_memLock);
        if (!ok) { Log("net: joiner %u left before the game accepted it", id); return; }
        SendWelcome(id);
        std::string nm = ToUtf8(name);
        std::vector<BYTE> joined; joined.push_back(IN_JOINED);
        joined.push_back((BYTE)(id & 0xFF)); joined.push_back((BYTE)(id >> 8));
        joined.insert(joined.end(), nm.begin(), nm.end());
        EnterCriticalSection(&m_memLock);
        for (WORD o : others) { auto m = m_members.find(o); if (m != m_members.end()) SendReliable(m->second, joined, true); }
        LeaveCriticalSection(&m_memLock);
        Log("net: member %u '%S' accepted", id, name.c_str());
        Job cp; cp.type = Job::CREATE_PLAYER; cp.id = id; Dispatch(cp);
    }

    // Host: refuse a joiner (the game said no, or the session is full). It
    // never became a player, so nobody gets a DESTROY_PLAYER for it.
    void RejectJoiner(WORD id, HRESULT hr, const std::vector<BYTE>& reply, const char* why) {
        std::vector<BYTE> p; p.push_back(IN_REJECT);
        p.insert(p.end(), (BYTE*)&hr, (BYTE*)&hr + 4);
        WORD n = (WORD)reply.size(); p.push_back((BYTE)(n & 0xFF)); p.push_back((BYTE)(n >> 8));
        p.insert(p.end(), reply.begin(), reply.end());
        EnterCriticalSection(&m_memLock);
        m_members.erase(id);
        Kicked k; k.pkt = p; k.lastSent = GetTickCount(); m_kicked[id] = k;
        LeaveCriticalSection(&m_memLock);
        Log("net: joiner %u refused (%s), hr=0x%08lX", id, why, hr);
        SendInner(id, p);
    }

    // Host, net thread: a refused or kicked member is still talking to us
    // (our answer may have been lost). Repeat it, rate-limited.
    bool AnswerKicked(WORD src) {
        std::vector<BYTE> pkt; bool hit = false;
        DWORD now = GetTickCount();
        EnterCriticalSection(&m_memLock);
        auto it = m_kicked.find(src);
        if (it != m_kicked.end()) {
            hit = true;
            if (now - it->second.lastSent >= KICK_RESEND_MS) { it->second.lastSent = now; pkt = it->second.pkt; }
        }
        LeaveCriticalSection(&m_memLock);
        if (!pkt.empty()) SendInner(src, pkt);
        return hit;
    }

    // Log the first few hundred traffic events in full, then a sample, so a
    // join sequence is always readable without drowning the log in gameplay.
    bool TrafficVerbose() {
        LONG n = InterlockedIncrement(&m_trafficLines);
        return n <= 300 || (n % 250) == 0;
    }

    // Test hook: move to a new local port, as a NAT that changed our mapping
    // would. Runs on the net thread between two receives.
    void SwapSocket() {
        SOCKET s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        sockaddr_in a{}; a.sin_family = AF_INET; a.sin_addr.s_addr = INADDR_ANY; a.sin_port = 0;
        if (s == INVALID_SOCKET || bind(s, (sockaddr*)&a, sizeof(a)) != 0) {
            if (s != INVALID_SOCKET) closesocket(s);
            Log("test: socket swap failed %d", WSAGetLastError());
            return;
        }
        DWORD tmo = 200; setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (char*)&tmo, sizeof(tmo));
        SOCKET old = m_sock; m_sock = s;
        Sleep(50);              // let sends that already picked up the old socket finish
        closesocket(old);
        Log("test: moved to a new local port (simulated NAT change)");
    }

    void NetLoop() {
        std::vector<BYTE> buf(65536);
        while (!m_netStop) {
            if (m_testRebind) { m_testRebind = false; SwapSocket(); }
            sockaddr_in from{}; int fl = sizeof(from);
            int n = recvfrom(m_sock, (char*)buf.data(), (int)buf.size(), 0, (sockaddr*)&from, &fl);
            Maintain();
            if (n < (int)sizeof(RlHdr)) continue;
            RlHdr h; memcpy(&h, buf.data(), sizeof(h));
            if (h.magic != RL_MAGIC) continue;
            m_lastRelayRx = GetTickCount();
            HandleRelay(h, buf.data() + sizeof(RlHdr), (DWORD)(n - sizeof(RlHdr)));
        }
    }

    // Periodic work on the net thread: hello retries, join timeout, relay
    // keepalive and silence, reliable retransmits.
    void Maintain() {
        DWORD now = GetTickCount();
        switch (m_state) {
        case ST_JOIN_HELLO:
            if (now - m_lastHelloMs >= HELLO_RETRY_MS) {
                BYTE role = ROLE_JOIN;
                SendRelay(RL_HELLO, m_session, 0, 0, &role, 1);
                m_lastHelloMs = now;
            }
            if (now - m_stateSince > g_joinTimeoutMs) FailConnect(DPNERR_NORESPONSE, "relay did not answer");
            return;
        case ST_JOIN_WELCOME:
            if (now - m_lastHelloMs >= HELLO_RETRY_MS) { SendHello(); m_lastHelloMs = now; }
            if (now - m_stateSince > g_joinTimeoutMs) FailConnect(DPNERR_NORESPONSE, "host did not welcome us");
            return;
        case ST_HOSTING:
        case ST_CONNECTED:
            if (m_session && now - m_lastPingMs >= RELAY_PING_MS) {
                DWORD stamp = now;   // a relay that echoes sends it back: round trip to the relay
                SendRelay(RL_PING, m_session, m_localId, 0, &stamp, sizeof(stamp));
                m_lastPingMs = now;
            }
            // Only a relay known to echo can be called silent; an older relay
            // never answers pings and would look dead between game packets.
            if (m_session && m_relayEchoes && now - m_lastRelayRx > g_relaySilenceMs) {
                SessionLost("relay silent", DPNERR_CONNECTIONLOST);
                return;
            }
            RetransmitTick(now);
            return;
        default:
            return;
        }
    }

    // Retransmit with a timeout from the measured round trip, doubling per
    // retry (up to 8x), and give up on a peer whose oldest send stays
    // unacknowledged for g_peerLostMs.
    void RetransmitTick(DWORD now) {
        std::vector<WORD> lost;
        EnterCriticalSection(&m_memLock);
        for (auto& kv : m_members) {
            Member& m = kv.second;
            DWORD rto = RtoOf(m);
            for (auto& u : m.unacked) {
                if (now - u.second.firstSend > g_peerLostMs) { lost.push_back(m.id); break; }
                DWORD shift = u.second.retries < 3 ? u.second.retries : 3;
                DWORD wait = rto << shift; if (wait > RETRANSMIT_CAP_MS) wait = RETRANSMIT_CAP_MS;
                if (now - u.second.lastSend < wait) continue;
                u.second.retries++; u.second.lastSend = now;
                memcpy(&u.second.pkt[STAMP_AT], &now, sizeof(DWORD));
                if (!DropRoll()) SendInner(m.id, u.second.pkt);
            }
        }
        LeaveCriticalSection(&m_memLock);
        for (WORD id : lost) {
            if (!m_isHost && id == 1) { SessionLost("host stopped acknowledging", DPNERR_CONNECTIONLOST); return; }
            RemoveMember(id, DPNDESTROYPLAYERREASON_CONNECTIONLOST, "reliable send gave up");
        }
    }

    void FailConnect(HRESULT hr, const char* why, const std::vector<BYTE>& reply = std::vector<BYTE>()) {
        Log("net: connect failed (%s), hr=0x%08lX", why, hr);
        // Leave the relay session as well, or our slot stays taken until the relay times it out.
        if (m_session && m_localId && m_sock != INVALID_SOCKET) SendRelay(RL_BYE, m_session, m_localId, 0, NULL, 0);
        m_state = ST_IDLE;
        Job cc; cc.type = Job::CONNECT_COMPLETE; cc.handle = m_connectHandle; cc.context = m_connectContext; cc.result = hr;
        cc.data = reply;
        Enqueue(cc);
    }

    // Adds a member if unknown. Returns true if it was new.
    bool AddMember(WORD id, const std::wstring& name, bool announce) {
        if (id == 0 || id == m_localId) return false;
        bool isNew = false;
        EnterCriticalSection(&m_memLock);
        auto it = m_members.find(id);
        if (it == m_members.end()) {
            Member m; m.id = id; m.name = name; m.announced = announce;
            m_members[id] = m; isNew = true;
        } else if (!name.empty() && it->second.name.empty()) {
            it->second.name = name;
        }
        LeaveCriticalSection(&m_memLock);
        if (isNew) Log("net: member %u '%S' joined", id, name.c_str());
        if (isNew && announce) { Job c; c.type = Job::CREATE_PLAYER; c.id = id; Enqueue(c); }
        return isNew;
    }

    void RemoveMember(WORD id, DWORD reason, const char* why) {
        bool existed = false, announced = false; void* ctx = NULL;
        EnterCriticalSection(&m_memLock);
        auto it = m_members.find(id);
        if (it != m_members.end()) { existed = true; announced = it->second.announced; ctx = it->second.ctx; m_members.erase(it); }
        LeaveCriticalSection(&m_memLock);
        if (!existed) return;
        Log("net: member %u left (%s)", id, why);
        if (announced) { Job d; d.type = Job::DESTROY_PLAYER; d.id = id; d.reason = reason; d.playerCtx = ctx; Enqueue(d); }
    }

    // DESTROY_PLAYER for every announced remote player and then the local
    // one, each with the context the game attached to it. Clears the roster.
    void DestroyEveryone(DWORD reason) {
        std::vector<std::pair<WORD, void*>> gone;
        EnterCriticalSection(&m_memLock);
        for (auto& kv : m_members) if (kv.second.announced) gone.push_back(std::make_pair(kv.first, kv.second.ctx));
        m_members.clear();
        LeaveCriticalSection(&m_memLock);
        for (auto& g : gone) { Job d; d.type = Job::DESTROY_PLAYER; d.id = g.first; d.reason = reason; d.playerCtx = g.second; Enqueue(d); }
        if (m_localId && !m_localDestroyed) {
            m_localDestroyed = true;
            Job d; d.type = Job::DESTROY_PLAYER; d.id = m_localId; d.reason = reason; d.playerCtx = m_localCtx; Enqueue(d);
        }
    }

    // The session is over for us (host gone, kicked, relay silent or
    // dropped us). A joiner mirrors DirectPlay: TERMINATE_SESSION first (with
    // the host's data when kicked), then DESTROY_PLAYER for everyone
    // including itself; the game is expected to Close() afterwards. The host
    // keeps its own game (stock dpnet never terminates the host over the
    // network) and only loses the remote players.
    void SessionLost(const char* why, HRESULT hr, const std::vector<BYTE>& data = std::vector<BYTE>()) {
        if (m_state != ST_CONNECTED && m_state != ST_HOSTING) return;
        Log("net: session lost (%s)", why);
        if (m_isHost) {
            std::vector<WORD> ids;
            EnterCriticalSection(&m_memLock);
            for (auto& kv : m_members) ids.push_back(kv.first);
            LeaveCriticalSection(&m_memLock);
            m_session = 0;      // nobody can reach this session any more; keep hosting solo
            for (WORD id : ids) RemoveMember(id, DPNDESTROYPLAYERREASON_CONNECTIONLOST, why);
            return;
        }
        m_state = ST_IDLE;
        m_session = 0;
        Job t; t.type = Job::TERMINATE; t.result = hr; t.data = data; Enqueue(t);
        DestroyEveryone(DPNDESTROYPLAYERREASON_SESSIONTERMINATED);
    }

    void TakeToken(const BYTE* body, DWORD len) {
        if (len >= sizeof(DWORD)) { memcpy(&m_token, body, sizeof(DWORD)); m_hasToken = true; }
    }

    void HandleRelay(const RlHdr& h, const BYTE* body, DWORD len) {
        switch (h.type) {
        case RL_HELLO_OK:
            if (m_state == ST_HOST_HELLO) {
                m_session = h.session; m_localId = h.src ? h.src : 1;
                TakeToken(body, len);
                SetEvent(m_helloEvt);
            } else if (m_state == ST_JOIN_HELLO && h.session == m_session) {
                m_localId = h.src;
                TakeToken(body, len);
                m_state = ST_JOIN_WELCOME; m_stateSince = GetTickCount();
                Log("net: relay accepted us as member %u, announcing to the session", m_localId);
                SendHello(); m_lastHelloMs = GetTickCount();
            } else if ((m_state == ST_HOSTING || m_state == ST_CONNECTED) && h.session == m_session && m_lastRebindMs) {
                Log("net: relay took our new address, still member %u", m_localId);
                m_lastRebindMs = 0;
            }
            break;
        case RL_PING:   // the relay's echo of our keepalive: [u32 tick we sent]
            if (len >= sizeof(DWORD) && h.session == m_session) {
                DWORD sent; memcpy(&sent, body, sizeof(DWORD));
                DWORD rtt = GetTickCount() - sent;
                if (rtt < 10000) {
                    m_relayRttMs = m_relayEchoes ? (m_relayRttMs * 7 + rtt) / 8 : rtt;
                    if (!m_relayRttMs) m_relayRttMs = 1;
                    if (!m_relayEchoes) Log("net: relay echoes keepalives, round trip %lu ms", rtt);
                    m_relayEchoes++;
                }
            }
            break;
        case RL_ERROR: {
            std::string reason((const char*)body, len);
            if (reason == "rebind" && h.session == m_session && (m_state == ST_HOSTING || m_state == ST_CONNECTED)) {
                // Our packets reach the relay from an address it does not
                // know (NAT changed the mapping). Prove who we are.
                if (!m_hasToken) { SessionLost("relay lost our address, no token to rebind", DPNERR_CONNECTIONLOST); break; }
                DWORD now = GetTickCount();
                if (now - m_lastRebindMs >= 500) {
                    BYTE p[1 + sizeof(DWORD)]; p[0] = ROLE_REBIND; memcpy(p + 1, &m_token, sizeof(DWORD));
                    SendRelay(RL_HELLO, m_session, m_localId, 0, p, sizeof(p));
                    m_lastRebindMs = now ? now : 1;
                    Log("net: relay does not know our address any more, rebinding as member %u", m_localId);
                }
                break;
            }
            Log("net: relay error '%s'", reason.c_str());
            if (m_state == ST_JOIN_HELLO || m_state == ST_JOIN_WELCOME) FailConnect(DPNERR_NOCONNECTION, reason.c_str());
            else SessionLost(reason.c_str(), DPNERR_CONNECTIONLOST);
            break;
        }
        case RL_LEFT: {
            bool bye = false;
            EnterCriticalSection(&m_memLock);
            auto it = m_members.find(h.src);
            if (it != m_members.end()) bye = it->second.saidBye;
            LeaveCriticalSection(&m_memLock);
            if (!m_isHost && h.src == 1) { SessionLost(bye ? "host left" : "host timed out at relay", DPNERR_HOSTTERMINATEDSESSION); break; }
            RemoveMember(h.src, bye ? DPNDESTROYPLAYERREASON_NORMAL : DPNDESTROYPLAYERREASON_CONNECTIONLOST,
                         bye ? "left" : "timed out at relay");
            break;
        }
        case RL_DATA:
            if (h.session == m_session && len >= 1 && h.src != m_localId) HandleInner(h.src, body[0], body + 1, len - 1);
            break;
        default:
            break;
        }
    }

    // Reliable control message from the host, delivered in order.
    void HandleControl(WORD src, const BYTE* p, DWORD n) {
        if (m_isHost || src != 1 || n < 3) return;
        WORD id = p[1] | (p[2] << 8);
        if (p[0] == IN_JOINED) AddMember(id, FromUtf8((const char*)p + 3, (int)(n - 3)), m_state == ST_CONNECTED);
        else if (p[0] == IN_DROPPED) RemoveMember(id, DPNDESTROYPLAYERREASON_HOSTDESTROYEDPLAYER, "kicked by the host");
    }

    void HandleInner(WORD src, BYTE type, const BYTE* body, DWORD len) {
        if (m_isHost && AnswerKicked(src)) return;   // refused or kicked: same answer again, nothing else
        switch (type) {
        case IN_HELLO: {
            // Only the host decides who joins; the others learn it from
            // IN_JOINED, so a refused joiner never shows up anywhere.
            if (!m_isHost || m_state != ST_HOSTING) break;
            std::wstring name = FromUtf8((const char*)body, (int)len);
            bool resend = false, isNew = false, full = false;
            EnterCriticalSection(&m_memLock);
            auto it = m_members.find(src);
            if (it != m_members.end()) {
                resend = it->second.announced;          // our WELCOME was lost
            } else {
                DWORD inSession = 1 + (DWORD)m_members.size();   // us, players, joiners being asked about
                DWORD maxp = m_appDesc.dwMaxPlayers ? m_appDesc.dwMaxPlayers : DEFAULT_MAX_PLAYERS;
                if (inSession >= maxp) full = true;
                else { Member m; m.id = src; m.name = name; m.indicating = true; m_members[src] = m; isNew = true; }
            }
            LeaveCriticalSection(&m_memLock);
            if (full) { RejectJoiner(src, DPNERR_SESSIONFULL, std::vector<BYTE>(), "session full"); break; }
            if (isNew) {
                Log("net: member %u '%S' asks to join", src, name.c_str());
                Job ic; ic.type = Job::INDICATE_CONNECT; ic.id = src; Enqueue(ic);
            } else if (resend) {
                SendWelcome(src);
            }
            break;
        }
        case IN_WELCOME: {
            if (m_state != ST_JOIN_WELCOME || src != 1) break;
            if (len < 1) break;
            BYTE n = body[0]; DWORD off = 1;
            std::vector<std::pair<WORD, std::wstring>> roster;
            for (BYTE i = 0; i < n && off + 3 <= len; ++i) {
                WORD id = body[off] | (body[off + 1] << 8); BYTE nl = body[off + 2]; off += 3;
                if (off + nl > len) break;
                roster.push_back(std::make_pair(id, FromUtf8((const char*)body + off, nl))); off += nl;
            }
            for (auto& r : roster) AddMember(r.first, r.second, false);
            std::vector<BYTE> reply;
            if (off + 9 <= len) {   // session description appended by the host
                DWORD flags, maxp; memcpy(&flags, body + off, 4); memcpy(&maxp, body + off + 4, 4);
                BYTE nl = body[off + 8]; off += 9;
                if (off + nl <= len) {
                    m_appDesc.dwFlags = flags; m_appDesc.dwMaxPlayers = maxp ? maxp : DEFAULT_MAX_PLAYERS;
                    if (nl) m_appName = FromUtf8((const char*)body + off, nl);
                    off += nl;
                    if (off + 2 <= len) {   // reply data from the host's INDICATE_CONNECT
                        WORD rl = body[off] | (body[off + 1] << 8); off += 2;
                        if (off + rl <= len) reply.assign(body + off, body + off + rl);
                    }
                }
            }
            m_state = ST_CONNECTED;
            Log("net: welcomed by host, %u other player(s), local player %u, session flags=0x%lX max=%lu name='%S'",
                (unsigned)roster.size(), m_localId, m_appDesc.dwFlags, m_appDesc.dwMaxPlayers, m_appName.c_str());
            // Delivery order to the game: local player, then everyone else,
            // then CONNECT_COMPLETE. Adjust here if the capture says otherwise.
            Job lp; lp.type = Job::CREATE_PLAYER; lp.id = m_localId; Enqueue(lp);
            std::vector<WORD> ids;
            EnterCriticalSection(&m_memLock);
            for (auto& kv : m_members) if (!kv.second.announced) { kv.second.announced = true; ids.push_back(kv.first); }
            LeaveCriticalSection(&m_memLock);
            for (WORD id : ids) { Job c; c.type = Job::CREATE_PLAYER; c.id = id; Enqueue(c); }
            Job cc; cc.type = Job::CONNECT_COMPLETE; cc.handle = m_connectHandle; cc.context = m_connectContext; cc.result = S_OK;
            cc.data = reply;
            Enqueue(cc);
            break;
        }
        case IN_REJECT: {
            if (m_isHost || src != 1 || m_state != ST_JOIN_WELCOME || len < 4) break;
            HRESULT hr; memcpy(&hr, body, 4);
            std::vector<BYTE> reply;
            if (len >= 6) {
                WORD rl = body[4] | (body[5] << 8);
                if (6 + (DWORD)rl <= len) reply.assign(body + 6, body + 6 + rl);
            }
            FailConnect(hr ? hr : DPNERR_HOSTREJECTEDCONNECTION, "refused by the host", reply);
            break;
        }
        case IN_KICK: {
            if (m_isHost || src != 1) break;
            std::vector<BYTE> data;
            if (len >= 2) {
                WORD dl = body[0] | (body[1] << 8);
                if (2 + (DWORD)dl <= len) data.assign(body + 2, body + 2 + dl);
            }
            if (m_state == ST_JOIN_WELCOME) { FailConnect(DPNERR_HOSTREJECTEDCONNECTION, "kicked while joining"); break; }
            if (m_state != ST_CONNECTED) break;
            // We are out: leave the relay at once so the others see us go.
            SendRelay(RL_BYE, m_session, m_localId, 0, NULL, 0);
            SessionLost("kicked by the host", DPNERR_HOSTTERMINATEDSESSION, data);
            break;
        }
        case IN_DATA: {
            if (len < 1 + sizeof(DWORD)) break;
            if (m_state != ST_HOSTING && m_state != ST_CONNECTED) break;
            BYTE kind = body[0];
            DWORD seq; memcpy(&seq, body + 1, sizeof(DWORD));
            DWORD head = 1 + sizeof(DWORD), stamp = 0;
            if (kind != KIND_UNRELIABLE) {
                if (len < head + sizeof(DWORD)) break;
                memcpy(&stamp, body + head, sizeof(DWORD)); head += sizeof(DWORD);
            }
            const BYTE* payload = body + head;
            DWORD plen = len - head;
            // Data from an id the host never announced creates no player. A
            // reliable send is not acked either, so it comes again once the
            // IN_JOINED for that player has arrived.
            if (!IsPlayer(src)) {
                if (TrafficVerbose()) Log("net: data from %u, who is no player here, ignored", src);
                break;
            }
            if (kind == KIND_UNRELIABLE) {
                Job rc; rc.type = Job::RECEIVE; rc.id = src; rc.data.assign(payload, payload + plen); Enqueue(rc);
                break;
            }
            std::vector<std::vector<BYTE>> deliver;   // [kind][payload]
            bool ack = false;
            EnterCriticalSection(&m_memLock);
            auto it = m_members.find(src);
            if (it != m_members.end()) {
                Member& m = it->second;
                if (seq == m.seqIn) {
                    std::vector<BYTE> d; d.reserve(1 + plen); d.push_back(kind); d.insert(d.end(), payload, payload + plen);
                    deliver.push_back(std::move(d));
                    m.seqIn++;
                    auto r = m.reorder.find(m.seqIn);
                    while (r != m.reorder.end()) {
                        deliver.push_back(std::move(r->second)); m.reorder.erase(r); m.seqIn++;
                        r = m.reorder.find(m.seqIn);
                    }
                    ack = true;
                } else if (seq < m.seqIn) {
                    ack = true;                                   // duplicate
                } else if (seq - m.seqIn <= RECV_WINDOW) {
                    if (!m.reorder.count(seq)) {
                        std::vector<BYTE>& d = m.reorder[seq];
                        d.push_back(kind); d.insert(d.end(), payload, payload + plen);
                    }
                    ack = true;
                }
            }
            LeaveCriticalSection(&m_memLock);
            if (ack) SendAck(src, seq, stamp);
            for (auto& d : deliver) {
                if (d[0] == KIND_CONTROL) { HandleControl(src, d.data() + 1, (DWORD)d.size() - 1); continue; }
                Job rc; rc.type = Job::RECEIVE; rc.id = src; rc.data.assign(d.begin() + 1, d.end()); Enqueue(rc);
            }
            break;
        }
        case IN_ACK: {
            if (len < sizeof(DWORD)) break;
            DWORD seq; memcpy(&seq, body, sizeof(DWORD));
            DWORD stamp = 0;
            if (len >= 2 * sizeof(DWORD)) memcpy(&stamp, body + sizeof(DWORD), sizeof(DWORD));
            EnterCriticalSection(&m_memLock);
            auto it = m_members.find(src);
            if (it != m_members.end()) {
                Member& m = it->second;
                auto u = m.unacked.find(seq);
                if (u != m.unacked.end()) {
                    // The echoed stamp tells which transmission got through,
                    // so retransmitted packets give a valid sample too.
                    DWORD s = GetTickCount() - stamp;
                    if (stamp && s < 30000) {
                        if (!m.hasRtt) { m.srtt = s; m.rttvar = s / 2; m.hasRtt = true; }
                        else {
                            DWORD d = s > m.srtt ? s - m.srtt : m.srtt - s;
                            m.rttvar = (3 * m.rttvar + d) / 4;
                            m.srtt = (7 * m.srtt + s) / 8;
                        }
                    }
                    m.unacked.erase(u);
                }
            }
            LeaveCriticalSection(&m_memLock);
            break;
        }
        case IN_BYE: {
            EnterCriticalSection(&m_memLock);
            auto it = m_members.find(src);
            if (it != m_members.end()) it->second.saidBye = true;
            LeaveCriticalSection(&m_memLock);
            if (!m_isHost && src == 1) SessionLost("host said goodbye", DPNERR_HOSTTERMINATEDSESSION);
            else RemoveMember(src, DPNDESTROYPLAYERREASON_NORMAL, "said goodbye");
            break;
        }
        default:
            break;
        }
    }

    // ---- state ---------------------------------------------------------------------------------

    LONG m_ref;
    PFNDPNMESSAGEHANDLER m_handler = NULL;
    PVOID m_context = NULL;
    PVOID m_playerContext = NULL;   // context the game passed to Host/Connect
    PVOID m_localCtx = NULL;        // context the game set in CREATE_PLAYER (local)
    bool m_localDestroyed = false;  // DESTROY_PLAYER for the local player already delivered
    std::wstring m_playerName = L"Player";
    DPN_APPLICATION_DESC m_appDesc{};
    std::wstring m_appName, m_appPass;

    CRITICAL_SECTION m_lock;        // job queue + kept buffers
    std::deque<Job> m_queue;
    std::map<DPNHANDLE, void*> m_kept;
    HANDLE m_thread = NULL;
    HANDLE m_wake = NULL;
    volatile bool m_stop = false;
    volatile LONG m_nextHandle = 1000;

    CRITICAL_SECTION m_memLock;     // members + reliable state
    std::map<WORD, Member> m_members;

    RelayTarget m_relay;
    sockaddr_in m_relayAddr{};
    SOCKET m_sock = INVALID_SOCKET;
    HANDLE m_netThread = NULL;
    volatile bool m_netStop = false;
    HANDLE m_helloEvt = NULL;
    volatile State m_state = ST_IDLE;
    DWORD m_stateSince = 0;
    DWORD m_lastHelloMs = 0;
    DWORD m_lastPingMs = 0;
    volatile DWORD m_lastRelayRx = 0;
    DWORD m_relayRttMs = 0;         // smoothed round trip to the relay (its RL_PING echo)
    DWORD m_relayEchoes = 0;        // echoes seen; silence detection only once the relay echoes at all
    DWORD m_token = 0;              // from HELLO_OK, proves who we are after a NAT address change
    bool m_hasToken = false;
    DWORD m_lastRebindMs = 0;
    volatile bool m_testRebind = false;   // test hook: move to a new local port (simulated NAT change)
    std::map<WORD, Kicked> m_kicked;      // host: refused/kicked members (under m_memLock)
    bool m_isHost = false;
    volatile DWORD m_session = 0;
    volatile WORD m_localId = 0;
    DPNHANDLE m_connectHandle = 0;
    void* m_connectContext = NULL;
    DPNHANDLE m_enumHandle = 0;     // last EnumHosts op, until the game cancels it
    void* m_enumContext = NULL;
    volatile LONG m_trafficLines = 0;
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
        Factory<RelayPeer>* f = new Factory<RelayPeer>();
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

// Test hooks (self-test only).
extern "C" void DpnTestSetDrop(int pct) { g_dropPct = pct; Log("DpnTestSetDrop: %d%%", pct); }
extern "C" void DpnTestSetJoinTimeout(int ms) { g_joinTimeoutMs = (DWORD)ms; Log("DpnTestSetJoinTimeout: %dms", ms); }
extern "C" void DpnTestSetRelaySilence(int ms) { g_relaySilenceMs = (DWORD)ms; Log("DpnTestSetRelaySilence: %dms", ms); }
extern "C" void DpnTestSetPeerLost(int ms) { g_peerLostMs = (DWORD)ms; Log("DpnTestSetPeerLost: %dms", ms); }
// The peer must come from this DLL's class factory (the self-test makes it).
extern "C" void DpnTestRebind(IDirectPlay8Peer* p) { if (p) static_cast<RelayPeer*>(p)->TestRebind(); }

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(inst);
        g_hModule = inst;
        LogInit();
        WSADATA wsa; WSAStartup(MAKEWORD(2, 2), &wsa);
        srand(GetTickCount());
        char exe[MAX_PATH] = {0};
        GetModuleFileNameA(NULL, exe, MAX_PATH);
        Log("=== dpnetrelay geladen (pid %lu) in %s ===", GetCurrentProcessId(), exe);
    }
    return TRUE;
}
