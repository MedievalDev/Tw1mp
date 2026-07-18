// Unused peer methods: reported as unsupported. Two Worlds does not
// call these in the solo-host path (verified by capture).
    STDMETHODIMP CancelAsyncOperation(DPNHANDLE hAsyncHandle, DWORD dwFlags) override { Log("Peer::CancelAsyncOperation (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP Connect(const DPN_APPLICATION_DESC* pdnAppDesc, IDirectPlay8Address* pHostAddr, IDirectPlay8Address* pDeviceInfo, const DPN_SECURITY_DESC* pdnSecurity, const DPN_SECURITY_CREDENTIALS* pdnCredentials, const void* pvUserConnectData, DWORD dwUserConnectDataSize, void* pvPlayerContext, void* pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override { Log("Peer::Connect (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP GetSendQueueInfo(DPNID dpnid, DWORD* pdwNumMsgs, DWORD* pdwNumBytes, DWORD dwFlags) override { Log("Peer::GetSendQueueInfo (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP GetApplicationDesc(DPN_APPLICATION_DESC* pAppDescBuffer, DWORD* pcbDataSize, DWORD dwFlags) override { Log("Peer::GetApplicationDesc (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP SetApplicationDesc(const DPN_APPLICATION_DESC* pad, DWORD dwFlags) override { Log("Peer::SetApplicationDesc (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP CreateGroup(const DPN_GROUP_INFO* pdpnGroupInfo, void* pvGroupContext, void* pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override { Log("Peer::CreateGroup (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP DestroyGroup(DPNID idGroup, PVOID pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override { Log("Peer::DestroyGroup (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP AddPlayerToGroup(DPNID idGroup, DPNID idClient, PVOID pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override { Log("Peer::AddPlayerToGroup (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP RemovePlayerFromGroup(DPNID idGroup, DPNID idClient, PVOID pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override { Log("Peer::RemovePlayerFromGroup (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP SetGroupInfo(DPNID dpnid, DPN_GROUP_INFO* pdpnGroupInfo,PVOID pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override { Log("Peer::SetGroupInfo (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP GetGroupInfo(DPNID dpnid, DPN_GROUP_INFO* pdpnGroupInfo, DWORD* pdwSize, DWORD dwFlags) override { Log("Peer::GetGroupInfo (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP EnumPlayersAndGroups(DPNID* prgdpnid, DWORD* pcdpnid, DWORD dwFlags) override { Log("Peer::EnumPlayersAndGroups (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP EnumGroupMembers(DPNID dpnid, DPNID* prgdpnid, DWORD* pcdpnid, DWORD dwFlags) override { Log("Peer::EnumGroupMembers (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP GetPeerAddress(DPNID dpnid, IDirectPlay8Address** pAddress, DWORD dwFlags) override { Log("Peer::GetPeerAddress (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP EnumHosts(PDPN_APPLICATION_DESC pApplicationDesc, IDirectPlay8Address* pAddrHost, IDirectPlay8Address* pDeviceInfo,PVOID pUserEnumData, DWORD dwUserEnumDataSize, DWORD dwEnumCount, DWORD dwRetryInterval, DWORD dwTimeOut,PVOID pvUserContext, DPNHANDLE* pAsyncHandle, DWORD dwFlags) override { Log("Peer::EnumHosts (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP DestroyPeer(DPNID dpnidClient, const void* pvDestroyData, DWORD dwDestroyDataSize, DWORD dwFlags) override { Log("Peer::DestroyPeer (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP GetPlayerContext(DPNID dpnid,PVOID* ppvPlayerContext, DWORD dwFlags) override { Log("Peer::GetPlayerContext (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP GetGroupContext(DPNID dpnid,PVOID* ppvGroupContext, DWORD dwFlags) override { Log("Peer::GetGroupContext (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP GetCaps(DPN_CAPS* pdpCaps, DWORD dwFlags) override { Log("Peer::GetCaps (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP SetCaps(const DPN_CAPS* pdpCaps, DWORD dwFlags) override { Log("Peer::SetCaps (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP SetSPCaps(const GUID* pguidSP, const DPN_SP_CAPS* pdpspCaps, DWORD dwFlags) override { Log("Peer::SetSPCaps (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP GetConnectionInfo(DPNID dpnid, DPN_CONNECTION_INFO* pdpConnectionInfo, DWORD dwFlags) override { Log("Peer::GetConnectionInfo (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP RegisterLobby(DPNHANDLE dpnHandle, struct IDirectPlay8LobbiedApplication* pIDP8LobbiedApplication, DWORD dwFlags) override { Log("Peer::RegisterLobby (stub)"); return DPNERR_UNSUPPORTED; }
    STDMETHODIMP TerminateSession(void* pvTerminateData, DWORD dwTerminateDataSize, DWORD dwFlags) override { Log("Peer::TerminateSession (stub)"); return DPNERR_UNSUPPORTED; }
