package com.xard.ytsystem.download

import com.xard.ytsystem.api.ApiException
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import javax.net.ssl.SSLException

object TransferErrors {
    fun message(error: Throwable, stage: String): String? {
        val causes = generateSequence(error) { it.cause }.take(8).toList()
        val raw = causes.joinToString(" ") { it.message.orEmpty() }
        return when {
            causes.any { it is SecurityException } -> "Permissão necessária. Selecione o arquivo novamente e autorize o acesso."
            raw.contains("ENOSPC", true) || raw.contains("no space", true) -> "Falta de espaço disponível. Libere armazenamento e tente novamente."
            causes.any { it is UnknownHostException || it is ConnectException } -> "Não foi possível conectar ao servidor. Verifique sua conexão."
            causes.any { it is SSLException } -> "Não foi possível estabelecer uma conexão segura. Confira a data e a hora do aparelho."
            causes.any { it is SocketTimeoutException } || raw.contains("timed out", true) -> "Download interrompido por tempo limite. Verifique a conexão e tente novamente."
            error is ApiException && error.statusCode == 429 -> "Muitas solicitações. Aguarde um momento e tente novamente."
            error is ApiException && error.statusCode >= 500 -> "Serviço temporariamente indisponível. Tente novamente em alguns minutos."
            raw.contains("Unsupported URL", true) -> "Formato ou link não suportado. Confira o endereço completo."
            stage == "saving" -> "Não foi possível salvar o arquivo em Downloads. Confira o espaço disponível e tente novamente."
            stage == "preparing" || stage == "tools_init" -> "Falha ao preparar o download. Confira o espaço disponível e atualize o aplicativo."
            else -> null
        }
    }
}
