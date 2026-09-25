# Slucss System para Android

Aplicativo Kotlin nativo. O fluxo normal solicita autorização à API e
executa yt-dlp/FFmpeg no aparelho, com uma tarefa por vez em foreground service.
Arquivos finais são publicados em `Downloads/Slucss` pelo MediaStore.

## Build debug

Requer JDK 17 e Android SDK 35:

```powershell
$env:JAVA_HOME = "C:\caminho\jdk-17"
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
.\gradlew.bat testDebugUnitTest assembleDebug
```

## Primeira release e assinatura

Na primeira versão, crie a chave somente uma vez:

```powershell
.\create-signing-key.ps1
```

A chave fica fora do repositório em
`%APPDATA%\SLUCSS YT SYSTEM\android-signing`. Faça backup seguro do `.jks`.
Todas as atualizações precisam usar esse mesmo arquivo, mesmo alias e senha.

Depois gere a release:

```powershell
.\build-release.ps1
```

O script gera quatro artefatos em `dist`:

- `Slucss-System-Installer-1.0.2.apk`: bootstrap leve para a primeira instalação;
- `Slucss-System-arm64-v8a.apk`: app completo para aparelhos ARM64;
- `Slucss-System-armeabi-v7a.apk`: app completo para aparelhos ARM de 32 bits;
- `Slucss-System.apk`: app completo universal para compatibilidade.

O bootstrap usa o mesmo pacote e certificado, com código menor que o app
completo. Ele valida o manifesto assinado, escolhe a arquitetura e, após uma
ação explícita do usuário, baixa o APK correto por streaming e mostra o
progresso. Antes de liberar “Atualizar agora”, confere SHA-256, pacote, versão e
certificado. A permissão para instalar por essa fonte só é solicitada após o
segundo toque, e a substituição sempre depende da confirmação oficial do
Android. O script nunca imprime a senha da chave.

> O `1.0.2` com progresso interno permanece candidato de homologação enquanto
> a classificação do Play Protect é contestada. O link público continua no
> `1.0.1` browser-only, que não declara `REQUEST_INSTALL_PACKAGES`.

## Publicação

1. Aumente `versionCode` e `versionName` em `app/build.gradle.kts`.
2. Gere a release e calcule o SHA-256 dos APKs completos.
3. Publique os três APKs completos em uma release externa imutável, usando uma
   tag Android separada, como `android-v1.1.0`. Publique o instalador interno
   apenas depois da homologação do Play Protect.
4. Configure `ANDROID_LATEST_VERSION`, `ANDROID_LATEST_VERSION_CODE`,
   `ANDROID_MINIMUM_VERSION_CODE`, `ANDROID_DOWNLOAD_URL`,
   `ANDROID_DOWNLOAD_SHA256`, os pares por arquitetura,
   `ANDROID_INSTALLER_URL`, `ANDROID_CHANGELOG_JSON`, `ANDROID_UPSCALE_ENABLED`,
   `ANDROID_UPSCALE_MINIMUM_VERSION_CODE` e as versões do engine/modelos no backend.
5. Reinicie o backend apenas depois que URL e hash do arquivo remoto estiverem
   validados. O APK não deve ser incluído no pacote de deploy do backend.

O aplicativo verifica o manifesto Ed25519 assinado em `/app/android/version`,
baixa por streaming, confere SHA-256 e abre o instalador oficial do Android.
