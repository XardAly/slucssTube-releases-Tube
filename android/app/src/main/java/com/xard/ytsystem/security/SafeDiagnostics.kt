package com.xard.ytsystem.security

/** Diagnostic text can leave the device through logs or the user's Copy action. */
object SafeDiagnostics {
    fun redact(value: String): String = value
        .replace(Regex("(?is)-----BEGIN .*?PRIVATE KEY-----.*?-----END .*?PRIVATE KEY-----"), "[private key removed]")
        .replace(Regex("(?i)https?://[^\\s<>\"']+"), "[URL removed]")
        .replace(Regex("(?i)(authorization|cookie|set-cookie)\\s*[:=][^\\r\\n]+"), "$1: [removed]")
        .replace(Regex("(?i)([\\w.-]*(?:token|secret|password|api[_-]?key)[\\w.-]*[\"']?\\s*[:=]\\s*)[^\\s,;]+"), "$1[removed]")
        .replace(Regex("eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+"), "[token removed]")
}
