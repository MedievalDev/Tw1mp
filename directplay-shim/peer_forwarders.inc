// Generated transparent forwarders for IDirectPlay8Peer. Initialize,
// GetLocalHostAddresses and GetPeerAddress are hand-written in the
// class (message-handler / address wrapping).
    STDMETHODIMP EnumServiceProviders(const GUID* pguidServiceProvider, const GUID* pguidApplication, DPN_SERVICE_PROVIDER_INFO* pSPInfoBuffer, DWORD* pcbEnumData, DWORD* pcReturned, DWORD dwFlags) override {
        Log("CALL Peer::EnumServiceProviders");
        return m_real->EnumServiceProviders(pguidServiceProvider, pguidApplication, pSPInfoBuffer, pcbEnumData, pcReturned, dwFlags);
    }
    STDMETHODIMP CancelAsyncOperation(DPNHANDLE hAsyncHandle, DWORD dwFlags) override {
        Log("CALL Peer::CancelAsyncOperation");
        return m_real->CancelAsyncOperation(hAsyncHandle, dwFlags);
    }
    STDMETHODIMP Connect(const DPN_APPLICATION_DESC* pdnAppDesc, IDirectPlay8Address* pHostAddr, IDirectPlay8Address* pDeviceInfo, const DPN_SECURITY_DESC* pdnSecurity, const DPN_SECURITY_CREDENTIALS* pdnCredentials, const void* pvUserConnectData, DWORD dwUserConnectDataSize, void* pvPlayerContext, void* pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override {
        Log("CALL Peer::Connect");
        return m_real->Connect(pdnAppDesc, pHostAddr, pDeviceInfo, pdnSecurity, pdnCredentials, pvUserConnectData, dwUserConnectDataSize, pvPlayerContext, pvAsyncContext, phAsyncHandle, dwFlags);
    }
    STDMETHODIMP SendTo(DPNID dpnid, const DPN_BUFFER_DESC* prgBufferDesc, DWORD cBufferDesc, DWORD dwTimeOut, void* pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override {
        Log("CALL Peer::SendTo");
        return m_real->SendTo(dpnid, prgBufferDesc, cBufferDesc, dwTimeOut, pvAsyncContext, phAsyncHandle, dwFlags);
    }
    STDMETHODIMP GetSendQueueInfo(DPNID dpnid, DWORD* pdwNumMsgs, DWORD* pdwNumBytes, DWORD dwFlags) override {
        Log("CALL Peer::GetSendQueueInfo");
        return m_real->GetSendQueueInfo(dpnid, pdwNumMsgs, pdwNumBytes, dwFlags);
    }
    STDMETHODIMP Host(const DPN_APPLICATION_DESC* pdnAppDesc, IDirectPlay8Address **prgpDeviceInfo, DWORD cDeviceInfo, const DPN_SECURITY_DESC* pdnSecurity, const DPN_SECURITY_CREDENTIALS* pdnCredentials, void* pvPlayerContext, DWORD dwFlags) override {
        Log("CALL Peer::Host");
        return m_real->Host(pdnAppDesc, prgpDeviceInfo, cDeviceInfo, pdnSecurity, pdnCredentials, pvPlayerContext, dwFlags);
    }
    STDMETHODIMP GetApplicationDesc(DPN_APPLICATION_DESC* pAppDescBuffer, DWORD* pcbDataSize, DWORD dwFlags) override {
        Log("CALL Peer::GetApplicationDesc");
        return m_real->GetApplicationDesc(pAppDescBuffer, pcbDataSize, dwFlags);
    }
    STDMETHODIMP SetApplicationDesc(const DPN_APPLICATION_DESC* pad, DWORD dwFlags) override {
        Log("CALL Peer::SetApplicationDesc");
        return m_real->SetApplicationDesc(pad, dwFlags);
    }
    STDMETHODIMP CreateGroup(const DPN_GROUP_INFO* pdpnGroupInfo, void* pvGroupContext, void* pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override {
        Log("CALL Peer::CreateGroup");
        return m_real->CreateGroup(pdpnGroupInfo, pvGroupContext, pvAsyncContext, phAsyncHandle, dwFlags);
    }
    STDMETHODIMP DestroyGroup(DPNID idGroup, PVOID pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override {
        Log("CALL Peer::DestroyGroup");
        return m_real->DestroyGroup(idGroup, pvAsyncContext, phAsyncHandle, dwFlags);
    }
    STDMETHODIMP AddPlayerToGroup(DPNID idGroup, DPNID idClient, PVOID pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override {
        Log("CALL Peer::AddPlayerToGroup");
        return m_real->AddPlayerToGroup(idGroup, idClient, pvAsyncContext, phAsyncHandle, dwFlags);
    }
    STDMETHODIMP RemovePlayerFromGroup(DPNID idGroup, DPNID idClient, PVOID pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override {
        Log("CALL Peer::RemovePlayerFromGroup");
        return m_real->RemovePlayerFromGroup(idGroup, idClient, pvAsyncContext, phAsyncHandle, dwFlags);
    }
    STDMETHODIMP SetGroupInfo(DPNID dpnid, DPN_GROUP_INFO* pdpnGroupInfo,PVOID pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override {
        Log("CALL Peer::SetGroupInfo");
        return m_real->SetGroupInfo(dpnid, pdpnGroupInfo, pvAsyncContext, phAsyncHandle, dwFlags);
    }
    STDMETHODIMP GetGroupInfo(DPNID dpnid, DPN_GROUP_INFO* pdpnGroupInfo, DWORD* pdwSize, DWORD dwFlags) override {
        Log("CALL Peer::GetGroupInfo");
        return m_real->GetGroupInfo(dpnid, pdpnGroupInfo, pdwSize, dwFlags);
    }
    STDMETHODIMP EnumPlayersAndGroups(DPNID* prgdpnid, DWORD* pcdpnid, DWORD dwFlags) override {
        Log("CALL Peer::EnumPlayersAndGroups");
        return m_real->EnumPlayersAndGroups(prgdpnid, pcdpnid, dwFlags);
    }
    STDMETHODIMP EnumGroupMembers(DPNID dpnid, DPNID* prgdpnid, DWORD* pcdpnid, DWORD dwFlags) override {
        Log("CALL Peer::EnumGroupMembers");
        return m_real->EnumGroupMembers(dpnid, prgdpnid, pcdpnid, dwFlags);
    }
    STDMETHODIMP SetPeerInfo(const DPN_PLAYER_INFO* pdpnPlayerInfo,PVOID pvAsyncContext, DPNHANDLE* phAsyncHandle, DWORD dwFlags) override {
        Log("CALL Peer::SetPeerInfo");
        return m_real->SetPeerInfo(pdpnPlayerInfo, pvAsyncContext, phAsyncHandle, dwFlags);
    }
    STDMETHODIMP GetPeerInfo(DPNID dpnid, DPN_PLAYER_INFO* pdpnPlayerInfo, DWORD* pdwSize, DWORD dwFlags) override {
        Log("CALL Peer::GetPeerInfo");
        return m_real->GetPeerInfo(dpnid, pdpnPlayerInfo, pdwSize, dwFlags);
    }
    STDMETHODIMP Close(DWORD dwFlags) override {
        Log("CALL Peer::Close");
        return m_real->Close(dwFlags);
    }
    STDMETHODIMP EnumHosts(PDPN_APPLICATION_DESC pApplicationDesc, IDirectPlay8Address* pAddrHost, IDirectPlay8Address* pDeviceInfo,PVOID pUserEnumData, DWORD dwUserEnumDataSize, DWORD dwEnumCount, DWORD dwRetryInterval, DWORD dwTimeOut,PVOID pvUserContext, DPNHANDLE* pAsyncHandle, DWORD dwFlags) override {
        Log("CALL Peer::EnumHosts");
        return m_real->EnumHosts(pApplicationDesc, pAddrHost, pDeviceInfo, pUserEnumData, dwUserEnumDataSize, dwEnumCount, dwRetryInterval, dwTimeOut, pvUserContext, pAsyncHandle, dwFlags);
    }
    STDMETHODIMP DestroyPeer(DPNID dpnidClient, const void* pvDestroyData, DWORD dwDestroyDataSize, DWORD dwFlags) override {
        Log("CALL Peer::DestroyPeer");
        return m_real->DestroyPeer(dpnidClient, pvDestroyData, dwDestroyDataSize, dwFlags);
    }
    STDMETHODIMP ReturnBuffer(DPNHANDLE hBufferHandle, DWORD dwFlags) override {
        Log("CALL Peer::ReturnBuffer");
        return m_real->ReturnBuffer(hBufferHandle, dwFlags);
    }
    STDMETHODIMP GetPlayerContext(DPNID dpnid,PVOID* ppvPlayerContext, DWORD dwFlags) override {
        Log("CALL Peer::GetPlayerContext");
        return m_real->GetPlayerContext(dpnid, ppvPlayerContext, dwFlags);
    }
    STDMETHODIMP GetGroupContext(DPNID dpnid,PVOID* ppvGroupContext, DWORD dwFlags) override {
        Log("CALL Peer::GetGroupContext");
        return m_real->GetGroupContext(dpnid, ppvGroupContext, dwFlags);
    }
    STDMETHODIMP GetCaps(DPN_CAPS* pdpCaps, DWORD dwFlags) override {
        Log("CALL Peer::GetCaps");
        return m_real->GetCaps(pdpCaps, dwFlags);
    }
    STDMETHODIMP SetCaps(const DPN_CAPS* pdpCaps, DWORD dwFlags) override {
        Log("CALL Peer::SetCaps");
        return m_real->SetCaps(pdpCaps, dwFlags);
    }
    STDMETHODIMP SetSPCaps(const GUID* pguidSP, const DPN_SP_CAPS* pdpspCaps, DWORD dwFlags) override {
        Log("CALL Peer::SetSPCaps");
        return m_real->SetSPCaps(pguidSP, pdpspCaps, dwFlags);
    }
    STDMETHODIMP GetSPCaps(const GUID* pguidSP, DPN_SP_CAPS* pdpspCaps, DWORD dwFlags) override {
        Log("CALL Peer::GetSPCaps");
        return m_real->GetSPCaps(pguidSP, pdpspCaps, dwFlags);
    }
    STDMETHODIMP GetConnectionInfo(DPNID dpnid, DPN_CONNECTION_INFO* pdpConnectionInfo, DWORD dwFlags) override {
        Log("CALL Peer::GetConnectionInfo");
        return m_real->GetConnectionInfo(dpnid, pdpConnectionInfo, dwFlags);
    }
    STDMETHODIMP RegisterLobby(DPNHANDLE dpnHandle, struct IDirectPlay8LobbiedApplication* pIDP8LobbiedApplication, DWORD dwFlags) override {
        Log("CALL Peer::RegisterLobby");
        return m_real->RegisterLobby(dpnHandle, pIDP8LobbiedApplication, dwFlags);
    }
    STDMETHODIMP TerminateSession(void* pvTerminateData, DWORD dwTerminateDataSize, DWORD dwFlags) override {
        Log("CALL Peer::TerminateSession");
        return m_real->TerminateSession(pvTerminateData, dwTerminateDataSize, dwFlags);
    }
