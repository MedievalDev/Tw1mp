// dpnetcapture - DirectPlay8 capture shim, second generation.
//
// A copy of directplay-shim/dpnetshim.cpp with much richer logging. The
// first shim recorded WHICH methods and messages Two Worlds uses when a
// player hosts alone. This one records WHAT it passes and gets back, so a
// two-player session played over the stock Windows dpnet.dll becomes a
// complete specification for the replacement:
//
//   * every IDirectPlay8Peer / IDirectPlay8Address call with its arguments
//     (dpnids, flags, sizes, URLs, application descriptions) and its HRESULT
//   * every message the real DirectPlay delivers to the game, with the
//     fields the game can act on (dpnids, contexts, result codes, addresses)
//     and the value the game's handler returns
//   * a hex prefix of every payload sent and received
//   * the thread id on each line, so reentrancy and ordering are visible
//
// Like its predecessor it is fully transparent: real C++ forwarders with a
// compiler-generated vtable (never a generic thunk), the real dpnet.dll does
// all the work. Registered per-user under WOW6432Node (register.ps1), no
// admin rights, nothing system-wide touched. The log is written next to
// the DLL (dpnetcapture.log) so it works on any machine, and it is flushed
// after every line so a crash loses nothing.
//
// Header: Wine dplay8.h/dpaddr.h (LGPL) - fine for this local capture tool.

#define WIN32_LEAN_AND_MEAN
#define INITGUID
#include <windows.h>
#include <dplay8.h>
#include <dpaddr.h>
#include <stdio.h>
#include <share.h>
#include <string>
#include <vector>
#include <set>

static HMODULE g_realDpnet = NULL;
static HMODULE g_hModule = NULL;
static LONG g_factoryRefs = 0;
static CRITICAL_SECTION g_logLock;
static bool g_logReady = false;
static FILE* g_logFile = NULL;
static char g_logPath[MAX_PATH] = {0};
static const DWORD HEX_PREFIX = 48;   // bytes of payload shown per line

// -- logging -----------------------------------------------------------

static void LogInit()
{
    if (g_logReady) return;
    InitializeCriticalSection(&g_logLock);
    if (g_hModule && GetModuleFileNameA(g_hModule, g_logPath, MAX_PATH)) {
        char* slash = strrchr(g_logPath, '\\');
        if (slash) strcpy_s(slash + 1, MAX_PATH - (slash + 1 - g_logPath), "dpnetcapture.log");
    } else {
        strcpy_s(g_logPath, MAX_PATH, "dpnetcapture.log");
    }
    // _fsopen with _SH_DENYNO: keep the file open for speed (flushed per
    // line), but let anyone read it while the game is running. fopen_s
    // would open it exclusively and lock out Get-Content/tail.
    g_logFile = _fsopen(g_logPath, "a", _SH_DENYNO);
    g_logReady = true;
}

static void Log(const char* fmt, ...)
{
    if (!g_logReady || !g_logFile) return;
    EnterCriticalSection(&g_logLock);
    SYSTEMTIME st; GetLocalTime(&st);
    fprintf(g_logFile, "[%02d:%02d:%02d.%03d t=%5lu] ", st.wHour, st.wMinute,
            st.wSecond, st.wMilliseconds, GetCurrentThreadId());
    va_list a; va_start(a, fmt); vfprintf(g_logFile, fmt, a); va_end(a);
    fprintf(g_logFile, "\n");
    fflush(g_logFile);
    LeaveCriticalSection(&g_logLock);
}

// -- formatting helpers ------------------------------------------------

static std::string Narrow(const WCHAR* w)
{
    if (!w) return "(null)";
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    if (n <= 1) return "";
    std::string s(n - 1, 0);
    WideCharToMultiByte(CP_UTF8, 0, w, -1, &s[0], n, NULL, NULL);
    return s;
}

static std::string GuidStr(const GUID& g)
{
    char b[64];
    sprintf_s(b, "{%08lX-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X}",
              g.Data1, g.Data2, g.Data3, g.Data4[0], g.Data4[1], g.Data4[2],
              g.Data4[3], g.Data4[4], g.Data4[5], g.Data4[6], g.Data4[7]);
    return b;
}

static std::string Hex(const void* p, DWORD n, DWORD max = HEX_PREFIX)
{
    if (!p || n == 0) return "";
    std::string s;
    const BYTE* b = (const BYTE*)p;
    DWORD m = n < max ? n : max;
    char t[4];
    for (DWORD i = 0; i < m; ++i) { sprintf_s(t, "%02X", b[i]); s += t; }
    if (n > max) s += "..";
    return s;
}

static std::string HrStr(HRESULT hr)
{
    char b[32];
    if (hr == S_OK) return "S_OK";
    if (hr == DPNSUCCESS_PENDING) return "PENDING";
    if (hr == DPNERR_BUFFERTOOSMALL) return "BUFFERTOOSMALL";
    if (hr == DPNERR_UNSUPPORTED) return "UNSUPPORTED";
    if (hr == DPNERR_INVALIDPARAM) return "INVALIDPARAM";
    if (hr == DPNERR_DOESNOTEXIST) return "DOESNOTEXIST";
    if (hr == DPNERR_INVALIDPLAYER) return "INVALIDPLAYER";
    if (hr == DPNERR_NORESPONSE) return "NORESPONSE";
    if (hr == DPNERR_NOCONNECTION) return "NOCONNECTION";
    if (hr == DPNERR_CONNECTIONLOST) return "CONNECTIONLOST";
    if (hr == DPNERR_HOSTTERMINATEDSESSION) return "HOSTTERMINATEDSESSION";
    if (hr == DPNERR_INVALIDHOSTADDRESS) return "INVALIDHOSTADDRESS";
    if (hr == DPNERR_SESSIONFULL) return "SESSIONFULL";
    if (hr == DPNERR_TIMEDOUT) return "TIMEDOUT";
    if (hr == DPNERR_CANNOTCANCEL) return "CANNOTCANCEL";
    if (hr == DPNERR_INVALIDHANDLE) return "INVALIDHANDLE";
    sprintf_s(b, "0x%08lX", hr);
    return b;
}

static std::string SendFlags(DWORD f)
{
    std::string s;
    if (f & DPNSEND_SYNC) s += "SYNC|";
    if (f & DPNSEND_NOCOPY) s += "NOCOPY|";
    if (f & DPNSEND_NOCOMPLETE) s += "NOCOMPLETE|";
    if (f & DPNSEND_COMPLETEONPROCESS) s += "COMPLETEONPROCESS|";
    if (f & DPNSEND_GUARANTEED) s += "GUARANTEED|";
    if (f & DPNSEND_NONSEQUENTIAL) s += "NONSEQUENTIAL|";
    if (f & DPNSEND_NOLOOPBACK) s += "NOLOOPBACK|";
    if (f & DPNSEND_PRIORITY_LOW) s += "PRIO_LOW|";
    if (f & DPNSEND_PRIORITY_HIGH) s += "PRIO_HIGH|";
    if (f & DPNSEND_COALESCE) s += "COALESCE|";
    if (!s.empty()) s.pop_back();
    char b[16]; sprintf_s(b, "0x%lX", f);
    return s.empty() ? std::string(b) : s + "(" + b + ")";
}

static std::string AppDesc(const DPN_APPLICATION_DESC* d)
{
    if (!d) return "appdesc=(null)";
    char b[256];
    sprintf_s(b, "appdesc{size=%lu flags=0x%lX max=%lu cur=%lu resv=%lu appresv=%lu",
              d->dwSize, d->dwFlags, d->dwMaxPlayers, d->dwCurrentPlayers,
              d->dwReservedDataSize, d->dwApplicationReservedDataSize);
    std::string s = b;
    s += " name='" + Narrow(d->pwszSessionName) + "'";
    s += " pass=" + std::string(d->pwszPassword ? "yes" : "no");
    s += " app=" + GuidStr(d->guidApplication);
    s += " inst=" + GuidStr(d->guidInstance);
    if (d->dwApplicationReservedDataSize && d->pvApplicationReservedData)
        s += " appresvhex=" + Hex(d->pvApplicationReservedData, d->dwApplicationReservedDataSize);
    s += "}";
    return s;
}

static std::string PlayerInfo(const DPN_PLAYER_INFO* p)
{
    if (!p) return "playerinfo=(null)";
    char b[128];
    sprintf_s(b, "playerinfo{size=%lu infoflags=0x%lX playerflags=0x%lX datasize=%lu",
              p->dwSize, p->dwInfoFlags, p->dwPlayerFlags, p->dwDataSize);
    std::string s = b;
    s += " name='" + Narrow(p->pwszName) + "'";
    if (p->dwDataSize && p->pvData) s += " datahex=" + Hex(p->pvData, p->dwDataSize);
    s += "}";
    return s;
}

// -- address wrapper (transparent, logs the game's URL traffic) --------

class AddrShim;
static CRITICAL_SECTION g_addrLock;
static std::set<AddrShim*> g_addrShims;

class AddrShim : public IDirectPlay8Address {
public:
    explicit AddrShim(IDirectPlay8Address* real) : m_ref(1), m_real(real) {
        EnterCriticalSection(&g_addrLock); g_addrShims.insert(this); LeaveCriticalSection(&g_addrLock);
    }
    ~AddrShim() {
        EnterCriticalSection(&g_addrLock); g_addrShims.erase(this); LeaveCriticalSection(&g_addrLock);
    }
    IDirectPlay8Address* Real() { return m_real; }

    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (IsEqualIID(riid, IID_IUnknown) || IsEqualIID(riid, IID_IDirectPlay8Address)) {
            *ppv = static_cast<IDirectPlay8Address*>(this); AddRef(); return S_OK;
        }
        return m_real->QueryInterface(riid, ppv);
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&m_ref); }
    STDMETHODIMP_(ULONG) Release() override {
        LONG r = InterlockedDecrement(&m_ref);
        if (r == 0) { m_real->Release(); delete this; }
        return r;
    }

    STDMETHODIMP BuildFromURLW(WCHAR* url) override {
        HRESULT hr = m_real->BuildFromURLW(url);
        Log("  Addr::BuildFromURLW '%s' -> %s", Narrow(url).c_str(), HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP BuildFromURLA(CHAR* url) override {
        HRESULT hr = m_real->BuildFromURLA(url);
        Log("  Addr::BuildFromURLA '%s' -> %s", url ? url : "(null)", HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP Duplicate(PDIRECTPLAY8ADDRESS* pp) override {
        HRESULT hr = m_real->Duplicate(pp);
        if (SUCCEEDED(hr) && pp && *pp) *pp = new AddrShim(*pp);
        Log("  Addr::Duplicate -> %s", HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP SetEqual(PDIRECTPLAY8ADDRESS a) override {
        HRESULT hr = m_real->SetEqual(Unwrap(a));
        Log("  Addr::SetEqual -> %s", HrStr(hr).c_str()); return hr;
    }
    STDMETHODIMP IsEqual(PDIRECTPLAY8ADDRESS a) override {
        HRESULT hr = m_real->IsEqual(Unwrap(a));
        Log("  Addr::IsEqual -> %s", HrStr(hr).c_str()); return hr;
    }
    STDMETHODIMP Clear() override { Log("  Addr::Clear"); return m_real->Clear(); }
    STDMETHODIMP GetURLW(WCHAR* url, PDWORD n) override {
        DWORD before = n ? *n : 0;
        HRESULT hr = m_real->GetURLW(url, n);
        if (hr == S_OK) Log("  Addr::GetURLW -> '%s'", Narrow(url).c_str());
        else Log("  Addr::GetURLW (buf=%lu need=%lu) -> %s", before, n ? *n : 0, HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP GetURLA(CHAR* url, PDWORD n) override {
        DWORD before = n ? *n : 0;
        HRESULT hr = m_real->GetURLA(url, n);
        if (hr == S_OK) Log("  Addr::GetURLA -> '%s'", url ? url : "");
        else Log("  Addr::GetURLA (buf=%lu need=%lu) -> %s", before, n ? *n : 0, HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP GetSP(GUID* g) override {
        HRESULT hr = m_real->GetSP(g);
        Log("  Addr::GetSP -> %s %s", HrStr(hr).c_str(), (hr == S_OK && g) ? GuidStr(*g).c_str() : "");
        return hr;
    }
    STDMETHODIMP GetUserData(LPVOID p, PDWORD n) override {
        HRESULT hr = m_real->GetUserData(p, n);
        Log("  Addr::GetUserData -> %s", HrStr(hr).c_str()); return hr;
    }
    STDMETHODIMP SetSP(const GUID* g) override {
        HRESULT hr = m_real->SetSP(g);
        Log("  Addr::SetSP %s -> %s", g ? GuidStr(*g).c_str() : "(null)", HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP SetUserData(const void* p, DWORD n) override {
        HRESULT hr = m_real->SetUserData(p, n);
        Log("  Addr::SetUserData size=%lu -> %s", n, HrStr(hr).c_str()); return hr;
    }
    STDMETHODIMP GetNumComponents(PDWORD n) override {
        HRESULT hr = m_real->GetNumComponents(n);
        Log("  Addr::GetNumComponents -> %s %lu", HrStr(hr).c_str(), n ? *n : 0); return hr;
    }
    STDMETHODIMP GetComponentByName(const WCHAR* name, LPVOID buf, PDWORD size, PDWORD type) override {
        HRESULT hr = m_real->GetComponentByName(name, buf, size, type);
        std::string v;
        if (hr == S_OK && buf && type) {
            if (*type == DPNA_DATATYPE_DWORD) { char b[16]; sprintf_s(b, "%lu", *(DWORD*)buf); v = b; }
            else if (*type == DPNA_DATATYPE_STRING) v = "'" + Narrow((const WCHAR*)buf) + "'";
            else v = "hex:" + Hex(buf, size ? *size : 0);
        }
        Log("  Addr::GetComponentByName '%s' -> %s %s", Narrow(name).c_str(), HrStr(hr).c_str(), v.c_str());
        return hr;
    }
    STDMETHODIMP GetComponentByIndex(DWORD i, WCHAR* name, PDWORD nlen, void* buf, PDWORD size, PDWORD type) override {
        HRESULT hr = m_real->GetComponentByIndex(i, name, nlen, buf, size, type);
        Log("  Addr::GetComponentByIndex %lu -> %s '%s'", i, HrStr(hr).c_str(),
            (hr == S_OK && name) ? Narrow(name).c_str() : "");
        return hr;
    }
    STDMETHODIMP AddComponent(const WCHAR* name, const void* data, DWORD size, DWORD type) override {
        HRESULT hr = m_real->AddComponent(name, data, size, type);
        std::string v;
        if (data) {
            if (type == DPNA_DATATYPE_DWORD) { char b[16]; sprintf_s(b, "%lu", *(const DWORD*)data); v = b; }
            else if (type == DPNA_DATATYPE_STRING) v = "'" + Narrow((const WCHAR*)data) + "'";
            else if (type == DPNA_DATATYPE_STRING_ANSI) v = std::string("'") + (const char*)data + "'";
            else v = "hex:" + Hex(data, size);
        }
        Log("  Addr::AddComponent '%s' type=%lu size=%lu %s -> %s", Narrow(name).c_str(), type, size,
            v.c_str(), HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP GetDevice(GUID* g) override {
        HRESULT hr = m_real->GetDevice(g);
        Log("  Addr::GetDevice -> %s %s", HrStr(hr).c_str(), (hr == S_OK && g) ? GuidStr(*g).c_str() : "");
        return hr;
    }
    STDMETHODIMP SetDevice(const GUID* g) override {
        HRESULT hr = m_real->SetDevice(g);
        Log("  Addr::SetDevice %s -> %s", g ? GuidStr(*g).c_str() : "(null)", HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP BuildFromDirectPlay4Address(LPVOID p, DWORD n) override {
        HRESULT hr = m_real->BuildFromDirectPlay4Address(p, n);
        Log("  Addr::BuildFromDirectPlay4Address -> %s", HrStr(hr).c_str()); return hr;
    }

    static IDirectPlay8Address* Unwrap(IDirectPlay8Address* a) {
        if (!a) return a;
        EnterCriticalSection(&g_addrLock);
        bool mine = g_addrShims.count((AddrShim*)a) != 0;
        LeaveCriticalSection(&g_addrLock);
        return mine ? ((AddrShim*)a)->m_real : a;
    }

private:
    LONG m_ref;
    IDirectPlay8Address* m_real;
};

static IDirectPlay8Address* WrapAddr(IDirectPlay8Address* a) { return a ? new AddrShim(a) : a; }

// Read an address' URL for our own log lines without generating a
// "CALL Addr::GetURLW" line (goes straight to the real object).
static std::string UrlOf(IDirectPlay8Address* a)
{
    if (!a) return "(null)";
    IDirectPlay8Address* real = AddrShim::Unwrap(a);
    DWORD n = 0;
    real->GetURLW(NULL, &n);
    if (n == 0 || n > 8192) return "(url?)";
    std::vector<WCHAR> buf(n);
    if (FAILED(real->GetURLW(buf.data(), &n))) return "(url-fail)";
    return Narrow(buf.data());
}

// -- DPN_MSGID_* names --------------------------------------------------

static const char* MsgName(DWORD id)
{
    switch (id) {
    case DPN_MSGID_CREATE_PLAYER:        return "CREATE_PLAYER";
    case DPN_MSGID_DESTROY_PLAYER:       return "DESTROY_PLAYER";
    case DPN_MSGID_ENUM_HOSTS_QUERY:     return "ENUM_HOSTS_QUERY";
    case DPN_MSGID_ENUM_HOSTS_RESPONSE:  return "ENUM_HOSTS_RESPONSE";
    case DPN_MSGID_INDICATE_CONNECT:     return "INDICATE_CONNECT";
    case DPN_MSGID_INDICATED_CONNECT_ABORTED: return "INDICATED_CONNECT_ABORTED";
    case DPN_MSGID_CONNECT_COMPLETE:     return "CONNECT_COMPLETE";
    case DPN_MSGID_RECEIVE:              return "RECEIVE";
    case DPN_MSGID_SEND_COMPLETE:        return "SEND_COMPLETE";
    case DPN_MSGID_TERMINATE_SESSION:    return "TERMINATE_SESSION";
    case DPN_MSGID_ADD_PLAYER_TO_GROUP:  return "ADD_PLAYER_TO_GROUP";
    case DPN_MSGID_REMOVE_PLAYER_FROM_GROUP: return "REMOVE_PLAYER_FROM_GROUP";
    case DPN_MSGID_CREATE_GROUP:         return "CREATE_GROUP";
    case DPN_MSGID_DESTROY_GROUP:        return "DESTROY_GROUP";
    case DPN_MSGID_APPLICATION_DESC:     return "APPLICATION_DESC";
    case DPN_MSGID_HOST_MIGRATE:         return "HOST_MIGRATE";
    case DPN_MSGID_RETURN_BUFFER:        return "RETURN_BUFFER";
    case DPN_MSGID_PEER_INFO:            return "PEER_INFO";
    case DPN_MSGID_ASYNC_OP_COMPLETE:    return "ASYNC_OP_COMPLETE";
    default:                             return "MSGID_?";
    }
}

// -- peer wrapper --------------------------------------------------------

class PeerShim : public IDirectPlay8Peer {
public:
    explicit PeerShim(IDirectPlay8Peer* real)
        : m_ref(1), m_real(real), m_gameHandler(NULL), m_gameContext(NULL) {}

    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (IsEqualIID(riid, IID_IUnknown) || IsEqualIID(riid, IID_IDirectPlay8Peer)) {
            *ppv = static_cast<IDirectPlay8Peer*>(this); AddRef(); return S_OK;
        }
        Log("Peer::QueryInterface %s (not peer)", GuidStr(riid).c_str());
        return m_real->QueryInterface(riid, ppv);
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&m_ref); }
    STDMETHODIMP_(ULONG) Release() override {
        LONG r = InterlockedDecrement(&m_ref);
        if (r == 0) { Log("Peer released (last ref)"); m_real->Release(); delete this; }
        return r;
    }

    // ---- message stream -------------------------------------------------

    STDMETHODIMP Initialize(PVOID ctx, PFNDPNMESSAGEHANDLER pfn, DWORD flags) override {
        m_gameHandler = pfn; m_gameContext = ctx;
        HRESULT hr = m_real->Initialize(this, &PeerShim::MsgThunk, flags);
        Log("CALL Peer::Initialize flags=0x%lX ctx=%p -> %s", flags, ctx, HrStr(hr).c_str());
        return hr;
    }

    static HRESULT WINAPI MsgThunk(PVOID ctx, DWORD id, PVOID msg) {
        PeerShim* self = static_cast<PeerShim*>(ctx);
        std::string in = self->DescribeMsg(id, msg);
        Log("  MSG %s %s", MsgName(id), in.c_str());
        HRESULT hr = self->m_gameHandler(self->m_gameContext, id, msg);
        std::string out = self->DescribeMsgAfter(id, msg);
        Log("  MSG %s -> game returned %s%s", MsgName(id), HrStr(hr).c_str(), out.c_str());
        return hr;
    }

    std::string DescribeMsg(DWORD id, PVOID msg) {
        char b[256];
        switch (id) {
        case DPN_MSGID_CREATE_PLAYER: {
            DPNMSG_CREATE_PLAYER* m = (DPNMSG_CREATE_PLAYER*)msg;
            sprintf_s(b, "dpnid=%lu ctx=%p", m->dpnidPlayer, m->pvPlayerContext); return b; }
        case DPN_MSGID_DESTROY_PLAYER: {
            DPNMSG_DESTROY_PLAYER* m = (DPNMSG_DESTROY_PLAYER*)msg;
            sprintf_s(b, "dpnid=%lu ctx=%p reason=%lu", m->dpnidPlayer, m->pvPlayerContext, m->dwReason); return b; }
        case DPN_MSGID_RECEIVE: {
            DPNMSG_RECEIVE* m = (DPNMSG_RECEIVE*)msg;
            sprintf_s(b, "from=%lu ctx=%p size=%lu buf=0x%lX hex=", m->dpnidSender, m->pvPlayerContext,
                      m->dwReceiveDataSize, (DWORD)m->hBufferHandle);
            return std::string(b) + Hex(m->pReceiveData, m->dwReceiveDataSize); }
        case DPN_MSGID_SEND_COMPLETE: {
            DPNMSG_SEND_COMPLETE* m = (DPNMSG_SEND_COMPLETE*)msg;
            sprintf_s(b, "op=0x%lX ctx=%p hr=%s sendtime=%lu", (DWORD)m->hAsyncOp, m->pvUserContext,
                      HrStr(m->hResultCode).c_str(), m->dwSendTime); return b; }
        case DPN_MSGID_CONNECT_COMPLETE: {
            DPNMSG_CONNECT_COMPLETE* m = (DPNMSG_CONNECT_COMPLETE*)msg;
            DPNID local = m->dwSize >= sizeof(DPNMSG_CONNECT_COMPLETE) ? m->dpnidLocal : 0;
            sprintf_s(b, "op=0x%lX ctx=%p hr=%s replysize=%lu localdpnid=%lu", (DWORD)m->hAsyncOp,
                      m->pvUserContext, HrStr(m->hResultCode).c_str(), m->dwApplicationReplyDataSize, local);
            return b; }
        case DPN_MSGID_INDICATE_CONNECT: {
            DPNMSG_INDICATE_CONNECT* m = (DPNMSG_INDICATE_CONNECT*)msg;
            sprintf_s(b, "connectdata=%lu maxreply=%lu ctx=%p", m->dwUserConnectDataSize,
                      m->dwReplyDataSize, m->pvPlayerContext);
            return std::string(b) + " player=" + UrlOf(m->pAddressPlayer) + " device=" + UrlOf(m->pAddressDevice); }
        case DPN_MSGID_ENUM_HOSTS_RESPONSE: {
            DPNMSG_ENUM_HOSTS_RESPONSE* m = (DPNMSG_ENUM_HOSTS_RESPONSE*)msg;
            sprintf_s(b, "ctx=%p rtt=%lu respsize=%lu", m->pvUserContext, m->dwRoundTripLatencyMS,
                      m->dwResponseDataSize);
            return std::string(b) + " sender=" + UrlOf(m->pAddressSender) + " device=" + UrlOf(m->pAddressDevice)
                   + " " + AppDesc(m->pApplicationDescription); }
        case DPN_MSGID_ENUM_HOSTS_QUERY: {
            DPNMSG_ENUM_HOSTS_QUERY* m = (DPNMSG_ENUM_HOSTS_QUERY*)msg;
            sprintf_s(b, "recvsize=%lu maxresp=%lu", m->dwReceivedDataSize, m->dwMaxResponseDataSize);
            return std::string(b) + " sender=" + UrlOf(m->pAddressSender); }
        case DPN_MSGID_TERMINATE_SESSION: {
            DPNMSG_TERMINATE_SESSION* m = (DPNMSG_TERMINATE_SESSION*)msg;
            sprintf_s(b, "hr=%s datasize=%lu", HrStr(m->hResultCode).c_str(), m->dwTerminateDataSize); return b; }
        case DPN_MSGID_HOST_MIGRATE: {
            DPNMSG_HOST_MIGRATE* m = (DPNMSG_HOST_MIGRATE*)msg;
            sprintf_s(b, "newhost=%lu ctx=%p", m->dpnidNewHost, m->pvPlayerContext); return b; }
        case DPN_MSGID_PEER_INFO: {
            DPNMSG_PEER_INFO* m = (DPNMSG_PEER_INFO*)msg;
            sprintf_s(b, "dpnid=%lu ctx=%p", m->dpnidPeer, m->pvPlayerContext); return b; }
        case DPN_MSGID_ASYNC_OP_COMPLETE: {
            DPNMSG_ASYNC_OP_COMPLETE* m = (DPNMSG_ASYNC_OP_COMPLETE*)msg;
            sprintf_s(b, "op=0x%lX ctx=%p hr=%s", (DWORD)m->hAsyncOp, m->pvUserContext, HrStr(m->hResultCode).c_str());
            return b; }
        case DPN_MSGID_RETURN_BUFFER: {
            DPNMSG_RETURN_BUFFER* m = (DPNMSG_RETURN_BUFFER*)msg;
            sprintf_s(b, "hr=%s buf=%p", HrStr(m->hResultCode).c_str(), m->pvBuffer); return b; }
        case DPN_MSGID_INDICATED_CONNECT_ABORTED: {
            DPNMSG_INDICATED_CONNECT_ABORTED* m = (DPNMSG_INDICATED_CONNECT_ABORTED*)msg;
            sprintf_s(b, "ctx=%p", m->pvPlayerContext); return b; }
        default: {
            DWORD sz = msg ? *(DWORD*)msg : 0;
            sprintf_s(b, "size=%lu", sz); return b; }
        }
    }

    // Fields the game fills in before returning (in/out members).
    std::string DescribeMsgAfter(DWORD id, PVOID msg) {
        char b[128];
        switch (id) {
        case DPN_MSGID_CREATE_PLAYER: {
            DPNMSG_CREATE_PLAYER* m = (DPNMSG_CREATE_PLAYER*)msg;
            sprintf_s(b, " ctx-set=%p", m->pvPlayerContext); return b; }
        case DPN_MSGID_INDICATE_CONNECT: {
            DPNMSG_INDICATE_CONNECT* m = (DPNMSG_INDICATE_CONNECT*)msg;
            sprintf_s(b, " ctx-set=%p replysize=%lu", m->pvPlayerContext, m->dwReplyDataSize); return b; }
        case DPN_MSGID_ENUM_HOSTS_QUERY: {
            DPNMSG_ENUM_HOSTS_QUERY* m = (DPNMSG_ENUM_HOSTS_QUERY*)msg;
            sprintf_s(b, " respsize=%lu", m->dwResponseDataSize); return b; }
        default: return "";
        }
    }

    // ---- session setup ------------------------------------------------

    STDMETHODIMP EnumServiceProviders(const GUID* sp, const GUID* app, DPN_SERVICE_PROVIDER_INFO* info,
                                      DWORD* cb, DWORD* count, DWORD flags) override {
        DWORD before = cb ? *cb : 0;
        HRESULT hr = m_real->EnumServiceProviders(sp, app, info, cb, count, flags);
        std::string names;
        if (hr == S_OK && info && count)
            for (DWORD i = 0; i < *count; ++i) names += " [" + GuidStr(info[i].guid) + " '" + Narrow(info[i].pwszName) + "']";
        Log("CALL Peer::EnumServiceProviders sp=%s app=%s buf=%lu flags=0x%lX -> %s count=%lu need=%lu%s",
            sp ? GuidStr(*sp).c_str() : "(all)", app ? GuidStr(*app).c_str() : "(null)", before, flags,
            HrStr(hr).c_str(), count ? *count : 0, cb ? *cb : 0, names.c_str());
        return hr;
    }
    STDMETHODIMP CancelAsyncOperation(DPNHANDLE h, DWORD flags) override {
        HRESULT hr = m_real->CancelAsyncOperation(h, flags);
        Log("CALL Peer::CancelAsyncOperation op=0x%lX flags=0x%lX -> %s", (DWORD)h, flags, HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP Connect(const DPN_APPLICATION_DESC* desc, IDirectPlay8Address* host, IDirectPlay8Address* dev,
                         const DPN_SECURITY_DESC* sec, const DPN_SECURITY_CREDENTIALS* cred,
                         const void* data, DWORD dataSize, void* playerCtx, void* asyncCtx,
                         DPNHANDLE* ph, DWORD flags) override {
        std::string hostUrl = UrlOf(host), devUrl = UrlOf(dev);
        HRESULT hr = m_real->Connect(desc, host, dev, sec, cred, data, dataSize, playerCtx, asyncCtx, ph, flags);
        Log("CALL Peer::Connect host='%s' device='%s' %s sec=%p cred=%p connectdata=%lu playerctx=%p asyncctx=%p "
            "flags=0x%lX -> %s op=0x%lX", hostUrl.c_str(), devUrl.c_str(), AppDesc(desc).c_str(), sec, cred,
            dataSize, playerCtx, asyncCtx, flags, HrStr(hr).c_str(), ph ? (DWORD)*ph : 0);
        return hr;
    }
    STDMETHODIMP Host(const DPN_APPLICATION_DESC* desc, IDirectPlay8Address** devs, DWORD ndev,
                      const DPN_SECURITY_DESC* sec, const DPN_SECURITY_CREDENTIALS* cred,
                      void* playerCtx, DWORD flags) override {
        std::string devUrls;
        for (DWORD i = 0; devs && i < ndev; ++i) devUrls += " dev" + std::to_string(i) + "='" + UrlOf(devs[i]) + "'";
        HRESULT hr = m_real->Host(desc, devs, ndev, sec, cred, playerCtx, flags);
        Log("CALL Peer::Host %s ndev=%lu%s sec=%p cred=%p playerctx=%p flags=0x%lX -> %s",
            AppDesc(desc).c_str(), ndev, devUrls.c_str(), sec, cred, playerCtx, flags, HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP EnumHosts(PDPN_APPLICATION_DESC desc, IDirectPlay8Address* host, IDirectPlay8Address* dev,
                           PVOID data, DWORD dataSize, DWORD count, DWORD retry, DWORD timeout,
                           PVOID ctx, DPNHANDLE* ph, DWORD flags) override {
        std::string hostUrl = UrlOf(host), devUrl = UrlOf(dev);
        HRESULT hr = m_real->EnumHosts(desc, host, dev, data, dataSize, count, retry, timeout, ctx, ph, flags);
        Log("CALL Peer::EnumHosts host='%s' device='%s' %s enumdata=%lu count=%lu retry=%lu timeout=%lu ctx=%p "
            "flags=0x%lX -> %s op=0x%lX", hostUrl.c_str(), devUrl.c_str(), AppDesc(desc).c_str(), dataSize,
            count, retry, timeout, ctx, flags, HrStr(hr).c_str(), ph ? (DWORD)*ph : 0);
        return hr;
    }
    STDMETHODIMP Close(DWORD flags) override {
        Log("CALL Peer::Close flags=0x%lX ...", flags);
        HRESULT hr = m_real->Close(flags);
        Log("CALL Peer::Close -> %s", HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP TerminateSession(void* data, DWORD size, DWORD flags) override {
        HRESULT hr = m_real->TerminateSession(data, size, flags);
        Log("CALL Peer::TerminateSession size=%lu flags=0x%lX -> %s", size, flags, HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP DestroyPeer(DPNID dpnid, const void* data, DWORD size, DWORD flags) override {
        HRESULT hr = m_real->DestroyPeer(dpnid, data, size, flags);
        Log("CALL Peer::DestroyPeer dpnid=%lu size=%lu flags=0x%lX -> %s", dpnid, size, flags, HrStr(hr).c_str());
        return hr;
    }

    // ---- data -----------------------------------------------------------

    STDMETHODIMP SendTo(DPNID dpnid, const DPN_BUFFER_DESC* bufs, DWORD nbufs, DWORD timeout,
                        void* ctx, DPNHANDLE* ph, DWORD flags) override {
        DWORD total = 0;
        for (DWORD i = 0; bufs && i < nbufs; ++i) total += bufs[i].dwBufferSize;
        std::string hex = (bufs && nbufs) ? Hex(bufs[0].pBufferData, bufs[0].dwBufferSize) : "";
        HRESULT hr = m_real->SendTo(dpnid, bufs, nbufs, timeout, ctx, ph, flags);
        Log("CALL Peer::SendTo to=%lu bufs=%lu size=%lu timeout=%lu ctx=%p flags=%s -> %s op=0x%lX hex=%s",
            dpnid, nbufs, total, timeout, ctx, SendFlags(flags).c_str(), HrStr(hr).c_str(),
            ph ? (DWORD)*ph : 0, hex.c_str());
        return hr;
    }
    STDMETHODIMP ReturnBuffer(DPNHANDLE h, DWORD flags) override {
        HRESULT hr = m_real->ReturnBuffer(h, flags);
        Log("CALL Peer::ReturnBuffer buf=0x%lX -> %s", (DWORD)h, HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP GetSendQueueInfo(DPNID dpnid, DWORD* nmsg, DWORD* nbytes, DWORD flags) override {
        HRESULT hr = m_real->GetSendQueueInfo(dpnid, nmsg, nbytes, flags);
        Log("CALL Peer::GetSendQueueInfo dpnid=%lu flags=0x%lX -> %s msgs=%lu bytes=%lu", dpnid, flags,
            HrStr(hr).c_str(), nmsg ? *nmsg : 0, nbytes ? *nbytes : 0);
        return hr;
    }

    // ---- player / session info -------------------------------------------

    STDMETHODIMP GetApplicationDesc(DPN_APPLICATION_DESC* buf, DWORD* cb, DWORD flags) override {
        DWORD before = cb ? *cb : 0;
        HRESULT hr = m_real->GetApplicationDesc(buf, cb, flags);
        Log("CALL Peer::GetApplicationDesc buf=%lu flags=0x%lX -> %s need=%lu %s", before, flags,
            HrStr(hr).c_str(), cb ? *cb : 0, hr == S_OK ? AppDesc(buf).c_str() : "");
        return hr;
    }
    STDMETHODIMP SetApplicationDesc(const DPN_APPLICATION_DESC* d, DWORD flags) override {
        HRESULT hr = m_real->SetApplicationDesc(d, flags);
        Log("CALL Peer::SetApplicationDesc %s flags=0x%lX -> %s", AppDesc(d).c_str(), flags, HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP SetPeerInfo(const DPN_PLAYER_INFO* info, PVOID ctx, DPNHANDLE* ph, DWORD flags) override {
        HRESULT hr = m_real->SetPeerInfo(info, ctx, ph, flags);
        Log("CALL Peer::SetPeerInfo %s ctx=%p flags=0x%lX -> %s op=0x%lX", PlayerInfo(info).c_str(), ctx, flags,
            HrStr(hr).c_str(), ph ? (DWORD)*ph : 0);
        return hr;
    }
    STDMETHODIMP GetPeerInfo(DPNID dpnid, DPN_PLAYER_INFO* info, DWORD* size, DWORD flags) override {
        DWORD before = size ? *size : 0;
        HRESULT hr = m_real->GetPeerInfo(dpnid, info, size, flags);
        Log("CALL Peer::GetPeerInfo dpnid=%lu buf=%lu flags=0x%lX -> %s need=%lu %s", dpnid, before, flags,
            HrStr(hr).c_str(), size ? *size : 0, hr == S_OK ? PlayerInfo(info).c_str() : "");
        return hr;
    }
    STDMETHODIMP GetPeerAddress(DPNID dpnid, IDirectPlay8Address** pa, DWORD flags) override {
        HRESULT hr = m_real->GetPeerAddress(dpnid, pa, flags);
        std::string url = (SUCCEEDED(hr) && pa && *pa) ? UrlOf(*pa) : "";
        if (SUCCEEDED(hr) && pa && *pa) *pa = WrapAddr(*pa);
        Log("CALL Peer::GetPeerAddress dpnid=%lu flags=0x%lX -> %s '%s'", dpnid, flags, HrStr(hr).c_str(), url.c_str());
        return hr;
    }
    STDMETHODIMP GetLocalHostAddresses(IDirectPlay8Address** pa, DWORD* n, DWORD flags) override {
        DWORD before = n ? *n : 0;
        HRESULT hr = m_real->GetLocalHostAddresses(pa, n, flags);
        std::string urls;
        if (SUCCEEDED(hr) && pa && n)
            for (DWORD i = 0; i < *n; ++i) { urls += " '" + UrlOf(pa[i]) + "'"; pa[i] = WrapAddr(pa[i]); }
        Log("CALL Peer::GetLocalHostAddresses buf=%lu flags=0x%lX -> %s count=%lu%s", before, flags,
            HrStr(hr).c_str(), n ? *n : 0, urls.c_str());
        return hr;
    }
    STDMETHODIMP GetPlayerContext(DPNID dpnid, PVOID* ctx, DWORD flags) override {
        HRESULT hr = m_real->GetPlayerContext(dpnid, ctx, flags);
        Log("CALL Peer::GetPlayerContext dpnid=%lu flags=0x%lX -> %s ctx=%p", dpnid, flags, HrStr(hr).c_str(),
            (hr == S_OK && ctx) ? *ctx : NULL);
        return hr;
    }
    STDMETHODIMP GetConnectionInfo(DPNID dpnid, DPN_CONNECTION_INFO* ci, DWORD flags) override {
        HRESULT hr = m_real->GetConnectionInfo(dpnid, ci, flags);
        Log("CALL Peer::GetConnectionInfo dpnid=%lu flags=0x%lX -> %s rtt=%lu", dpnid, flags, HrStr(hr).c_str(),
            (hr == S_OK && ci) ? ci->dwRoundTripLatencyMS : 0);
        return hr;
    }
    STDMETHODIMP EnumPlayersAndGroups(DPNID* ids, DWORD* n, DWORD flags) override {
        DWORD before = n ? *n : 0;
        HRESULT hr = m_real->EnumPlayersAndGroups(ids, n, flags);
        std::string list;
        if (hr == S_OK && ids && n) for (DWORD i = 0; i < *n; ++i) list += " " + std::to_string(ids[i]);
        Log("CALL Peer::EnumPlayersAndGroups buf=%lu flags=0x%lX -> %s count=%lu ids=%s", before, flags,
            HrStr(hr).c_str(), n ? *n : 0, list.c_str());
        return hr;
    }
    STDMETHODIMP GetCaps(DPN_CAPS* caps, DWORD flags) override {
        HRESULT hr = m_real->GetCaps(caps, flags);
        if (hr == S_OK && caps)
            Log("CALL Peer::GetCaps flags=0x%lX -> S_OK caps{flags=0x%lX connecttimeout=%lu retries=%lu keepalive=%lu}",
                flags, caps->dwFlags, caps->dwConnectTimeout, caps->dwConnectRetries, caps->dwTimeoutUntilKeepAlive);
        else Log("CALL Peer::GetCaps flags=0x%lX -> %s", flags, HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP SetCaps(const DPN_CAPS* caps, DWORD flags) override {
        HRESULT hr = m_real->SetCaps(caps, flags);
        if (caps)
            Log("CALL Peer::SetCaps caps{flags=0x%lX connecttimeout=%lu retries=%lu keepalive=%lu} flags=0x%lX -> %s",
                caps->dwFlags, caps->dwConnectTimeout, caps->dwConnectRetries, caps->dwTimeoutUntilKeepAlive,
                flags, HrStr(hr).c_str());
        else Log("CALL Peer::SetCaps (null) -> %s", HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP GetSPCaps(const GUID* sp, DPN_SP_CAPS* caps, DWORD flags) override {
        HRESULT hr = m_real->GetSPCaps(sp, caps, flags);
        if (hr == S_OK && caps)
            Log("CALL Peer::GetSPCaps sp=%s -> S_OK spcaps{flags=0x%lX threads=%lu enumcount=%lu retry=%lu "
                "timeout=%lu maxenum=%lu bufs=%lu sysbuf=%lu}", sp ? GuidStr(*sp).c_str() : "(null)",
                caps->dwFlags, caps->dwNumThreads, caps->dwDefaultEnumCount, caps->dwDefaultEnumRetryInterval,
                caps->dwDefaultEnumTimeout, caps->dwMaxEnumPayloadSize, caps->dwBuffersPerThread,
                caps->dwSystemBufferSize);
        else Log("CALL Peer::GetSPCaps sp=%s -> %s", sp ? GuidStr(*sp).c_str() : "(null)", HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP SetSPCaps(const GUID* sp, const DPN_SP_CAPS* caps, DWORD flags) override {
        HRESULT hr = m_real->SetSPCaps(sp, caps, flags);
        if (caps)
            Log("CALL Peer::SetSPCaps sp=%s spcaps{flags=0x%lX threads=%lu sysbuf=%lu} -> %s",
                sp ? GuidStr(*sp).c_str() : "(null)", caps->dwFlags, caps->dwNumThreads,
                caps->dwSystemBufferSize, HrStr(hr).c_str());
        else Log("CALL Peer::SetSPCaps (null) -> %s", HrStr(hr).c_str());
        return hr;
    }
    STDMETHODIMP RegisterLobby(DPNHANDLE h, struct IDirectPlay8LobbiedApplication* app, DWORD flags) override {
        HRESULT hr = m_real->RegisterLobby(h, app, flags);
        Log("CALL Peer::RegisterLobby h=0x%lX flags=0x%lX -> %s", (DWORD)h, flags, HrStr(hr).c_str());
        return hr;
    }

    // ---- groups (not expected, logged if they ever show up) --------------

    STDMETHODIMP CreateGroup(const DPN_GROUP_INFO* gi, void* gctx, void* actx, DPNHANDLE* ph, DWORD flags) override {
        HRESULT hr = m_real->CreateGroup(gi, gctx, actx, ph, flags);
        Log("CALL Peer::CreateGroup flags=0x%lX -> %s", flags, HrStr(hr).c_str()); return hr;
    }
    STDMETHODIMP DestroyGroup(DPNID g, PVOID actx, DPNHANDLE* ph, DWORD flags) override {
        HRESULT hr = m_real->DestroyGroup(g, actx, ph, flags);
        Log("CALL Peer::DestroyGroup group=%lu -> %s", g, HrStr(hr).c_str()); return hr;
    }
    STDMETHODIMP AddPlayerToGroup(DPNID g, DPNID c, PVOID actx, DPNHANDLE* ph, DWORD flags) override {
        HRESULT hr = m_real->AddPlayerToGroup(g, c, actx, ph, flags);
        Log("CALL Peer::AddPlayerToGroup group=%lu player=%lu -> %s", g, c, HrStr(hr).c_str()); return hr;
    }
    STDMETHODIMP RemovePlayerFromGroup(DPNID g, DPNID c, PVOID actx, DPNHANDLE* ph, DWORD flags) override {
        HRESULT hr = m_real->RemovePlayerFromGroup(g, c, actx, ph, flags);
        Log("CALL Peer::RemovePlayerFromGroup group=%lu player=%lu -> %s", g, c, HrStr(hr).c_str()); return hr;
    }
    STDMETHODIMP SetGroupInfo(DPNID g, DPN_GROUP_INFO* gi, PVOID actx, DPNHANDLE* ph, DWORD flags) override {
        HRESULT hr = m_real->SetGroupInfo(g, gi, actx, ph, flags);
        Log("CALL Peer::SetGroupInfo group=%lu -> %s", g, HrStr(hr).c_str()); return hr;
    }
    STDMETHODIMP GetGroupInfo(DPNID g, DPN_GROUP_INFO* gi, DWORD* size, DWORD flags) override {
        HRESULT hr = m_real->GetGroupInfo(g, gi, size, flags);
        Log("CALL Peer::GetGroupInfo group=%lu -> %s", g, HrStr(hr).c_str()); return hr;
    }
    STDMETHODIMP EnumGroupMembers(DPNID g, DPNID* ids, DWORD* n, DWORD flags) override {
        HRESULT hr = m_real->EnumGroupMembers(g, ids, n, flags);
        Log("CALL Peer::EnumGroupMembers group=%lu -> %s", g, HrStr(hr).c_str()); return hr;
    }
    STDMETHODIMP GetGroupContext(DPNID g, PVOID* ctx, DWORD flags) override {
        HRESULT hr = m_real->GetGroupContext(g, ctx, flags);
        Log("CALL Peer::GetGroupContext group=%lu -> %s", g, HrStr(hr).c_str()); return hr;
    }

private:
    LONG m_ref;
    IDirectPlay8Peer* m_real;
    PFNDPNMESSAGEHANDLER m_gameHandler;
    PVOID m_gameContext;
};

// -- class factory ---------------------------------------------------------

typedef HRESULT (WINAPI *PFN_DllGetClassObject)(REFCLSID, REFIID, LPVOID*);

class ShimFactory : public IClassFactory {
public:
    ShimFactory(IClassFactory* real) : m_ref(1), m_real(real) {}

    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (IsEqualIID(riid, IID_IUnknown) || IsEqualIID(riid, IID_IClassFactory)) {
            *ppv = static_cast<IClassFactory*>(this); AddRef(); return S_OK;
        }
        return m_real->QueryInterface(riid, ppv);
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&m_ref); }
    STDMETHODIMP_(ULONG) Release() override {
        LONG r = InterlockedDecrement(&m_ref);
        if (r == 0) { m_real->Release(); InterlockedDecrement(&g_factoryRefs); delete this; }
        return r;
    }
    STDMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** ppv) override {
        HRESULT hr = m_real->CreateInstance(outer, riid, ppv);
        const char* what = "other";
        if (SUCCEEDED(hr) && ppv && *ppv) {
            if (IsEqualIID(riid, IID_IDirectPlay8Peer)) { *ppv = new PeerShim((IDirectPlay8Peer*)*ppv); what = "Peer"; }
            else if (IsEqualIID(riid, IID_IDirectPlay8Address)) { *ppv = WrapAddr((IDirectPlay8Address*)*ppv); what = "Address"; }
        }
        Log("CreateInstance riid=%s -> %s (%s)", GuidStr(riid).c_str(), HrStr(hr).c_str(), what);
        return hr;
    }
    STDMETHODIMP LockServer(BOOL lock) override { return m_real->LockServer(lock); }

private:
    LONG m_ref;
    IClassFactory* m_real;
};

// -- exports -----------------------------------------------------------------

STDAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, LPVOID* ppv)
{
    LogInit();
    const char* cls = IsEqualGUID(rclsid, CLSID_DirectPlay8Peer) ? "Peer"
                    : IsEqualGUID(rclsid, CLSID_DirectPlay8Address) ? "Address"
                    : IsEqualGUID(rclsid, CLSID_DirectPlay8Client) ? "Client"
                    : IsEqualGUID(rclsid, CLSID_DirectPlay8Server) ? "Server" : "other";
    Log("DllGetClassObject clsid=%s (%s) riid=%s", GuidStr(rclsid).c_str(), cls, GuidStr(riid).c_str());

    if (!g_realDpnet) {
        char path[MAX_PATH];
        GetSystemDirectoryA(path, MAX_PATH);   // SysWOW64 for a 32-bit process
        strcat_s(path, MAX_PATH, "\\dpnet.dll");
        g_realDpnet = LoadLibraryA(path);
        Log("  loaded real dpnet: %s -> %p", path, g_realDpnet);
    }
    if (!g_realDpnet) return CLASS_E_CLASSNOTAVAILABLE;

    PFN_DllGetClassObject real = (PFN_DllGetClassObject)GetProcAddress(g_realDpnet, "DllGetClassObject");
    if (!real) return CLASS_E_CLASSNOTAVAILABLE;

    HRESULT hr = real(rclsid, riid, ppv);
    if (SUCCEEDED(hr) && IsEqualIID(riid, IID_IClassFactory) && ppv && *ppv) {
        if (IsEqualGUID(rclsid, CLSID_DirectPlay8Peer) || IsEqualGUID(rclsid, CLSID_DirectPlay8Address)) {
            *ppv = new ShimFactory((IClassFactory*)*ppv);
            InterlockedIncrement(&g_factoryRefs);
        }
    }
    return hr;
}

STDAPI DllCanUnloadNow(void)
{
    return g_factoryRefs == 0 ? S_OK : S_FALSE;
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(inst);
        g_hModule = inst;
        InitializeCriticalSection(&g_addrLock);
        LogInit();
        char exe[MAX_PATH] = {0};
        GetModuleFileNameA(NULL, exe, MAX_PATH);
        Log("=== dpnetcapture geladen (pid %lu) in %s ===", GetCurrentProcessId(), exe);
    }
    return TRUE;
}
