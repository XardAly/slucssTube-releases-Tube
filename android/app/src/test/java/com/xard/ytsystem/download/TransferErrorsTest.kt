package com.xard.ytsystem.download

import com.xard.ytsystem.api.ApiException
import com.xard.ytsystem.security.SafeDiagnostics
import org.junit.Assert.*
import org.junit.Test
import java.net.SocketTimeoutException
import java.net.UnknownHostException

class TransferErrorsTest {
    @Test fun errorsIdentifyRecoverableCauses() {
        assertTrue(TransferErrors.message(Exception("wrapper", UnknownHostException()), "processing")!!.contains("conectar"))
        assertTrue(TransferErrors.message(SocketTimeoutException(), "processing")!!.contains("interrompido"))
        assertTrue(TransferErrors.message(Exception("ENOSPC"), "saving")!!.contains("espaço"))
        assertTrue(TransferErrors.message(ApiException(503, "internal"), "authorizing")!!.contains("indisponível"))
        assertTrue(TransferErrors.message(SecurityException(), "saving")!!.contains("Permissão"))
        assertTrue(TransferErrors.message(Exception(), "saving")!!.contains("salvar"))
    }

    @Test fun diagnosticsRemoveCredentialsAndSignedUrls() {
        val text = SafeDiagnostics.redact("ERROR 403 https://user:password@cdn.example/v?sig=private\nAuthorization: Bearer abc\nCookie: sid=private\nJWT_SECRET=private\napi_key=private")
        assertFalse(text.contains("private"))
        assertFalse(text.contains("password"))
        assertFalse(text.contains("Bearer abc"))
        assertTrue(text.contains("ERROR 403"))
    }
}
