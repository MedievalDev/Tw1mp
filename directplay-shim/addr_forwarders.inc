// Generated transparent forwarders for IDirectPlay8Address.
    STDMETHODIMP BuildFromURLW(WCHAR* pwszSourceURL) override {
        Log("  CALL Addr::BuildFromURLW");
        return m_real->BuildFromURLW(pwszSourceURL);
    }
    STDMETHODIMP BuildFromURLA(CHAR* pszSourceURL) override {
        Log("  CALL Addr::BuildFromURLA");
        return m_real->BuildFromURLA(pszSourceURL);
    }
    STDMETHODIMP Duplicate(PDIRECTPLAY8ADDRESS* ppdpaNewAddress) override {
        Log("  CALL Addr::Duplicate");
        return m_real->Duplicate(ppdpaNewAddress);
    }
    STDMETHODIMP SetEqual(PDIRECTPLAY8ADDRESS pdpaAddress) override {
        Log("  CALL Addr::SetEqual");
        return m_real->SetEqual(pdpaAddress);
    }
    STDMETHODIMP IsEqual(PDIRECTPLAY8ADDRESS pdpaAddress) override {
        Log("  CALL Addr::IsEqual");
        return m_real->IsEqual(pdpaAddress);
    }
    STDMETHODIMP Clear() override {
        Log("  CALL Addr::Clear");
        return m_real->Clear();
    }
    STDMETHODIMP GetURLW(WCHAR* pwszURL, PDWORD pdwNumChars) override {
        Log("  CALL Addr::GetURLW");
        return m_real->GetURLW(pwszURL, pdwNumChars);
    }
    STDMETHODIMP GetURLA(CHAR* pszURL, PDWORD pdwNumChars) override {
        Log("  CALL Addr::GetURLA");
        return m_real->GetURLA(pszURL, pdwNumChars);
    }
    STDMETHODIMP GetSP(GUID* pguidSP) override {
        Log("  CALL Addr::GetSP");
        return m_real->GetSP(pguidSP);
    }
    STDMETHODIMP GetUserData(LPVOID pvUserData, PDWORD pdwBufferSize) override {
        Log("  CALL Addr::GetUserData");
        return m_real->GetUserData(pvUserData, pdwBufferSize);
    }
    STDMETHODIMP SetSP(const GUID* pguidSP) override {
        Log("  CALL Addr::SetSP");
        return m_real->SetSP(pguidSP);
    }
    STDMETHODIMP SetUserData(const void* pvUserData, DWORD dwDataSize) override {
        Log("  CALL Addr::SetUserData");
        return m_real->SetUserData(pvUserData, dwDataSize);
    }
    STDMETHODIMP GetNumComponents(PDWORD pdwNumComponents) override {
        Log("  CALL Addr::GetNumComponents");
        return m_real->GetNumComponents(pdwNumComponents);
    }
    STDMETHODIMP GetComponentByName(const WCHAR* pwszName, LPVOID pvBuffer, PDWORD pdwBufferSize, PDWORD pdwDataType) override {
        Log("  CALL Addr::GetComponentByName");
        return m_real->GetComponentByName(pwszName, pvBuffer, pdwBufferSize, pdwDataType);
    }
    STDMETHODIMP GetComponentByIndex(DWORD dwComponentID, WCHAR* pwszName, PDWORD pdwNameLen, void* pvBuffer, PDWORD pdwBufferSize, PDWORD pdwDataType) override {
        Log("  CALL Addr::GetComponentByIndex");
        return m_real->GetComponentByIndex(dwComponentID, pwszName, pdwNameLen, pvBuffer, pdwBufferSize, pdwDataType);
    }
    STDMETHODIMP AddComponent(const WCHAR* pwszName, const void* lpvData, DWORD dwDataSize, DWORD dwDataType) override {
        Log("  CALL Addr::AddComponent");
        return m_real->AddComponent(pwszName, lpvData, dwDataSize, dwDataType);
    }
    STDMETHODIMP GetDevice(GUID* pDevGuid) override {
        Log("  CALL Addr::GetDevice");
        return m_real->GetDevice(pDevGuid);
    }
    STDMETHODIMP SetDevice(const GUID* devGuid) override {
        Log("  CALL Addr::SetDevice");
        return m_real->SetDevice(devGuid);
    }
    STDMETHODIMP BuildFromDirectPlay4Address(LPVOID pvAddress, DWORD dwDataSize) override {
        Log("  CALL Addr::BuildFromDirectPlay4Address");
        return m_real->BuildFromDirectPlay4Address(pvAddress, dwDataSize);
    }
