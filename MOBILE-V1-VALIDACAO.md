# Mobile V1 — relatório de entrega

**1.2.2 (22) → 1.3.0 (23).** APK release assinado com o certificado oficial
`2475687690fc94a3d27ab6206f804f963a91ebb781d96ca67a223f805dd8a775`.

## Bugs e correções

| Problema / causa | Correção |
|---|---|
| Parciais apagados ao iniciar e finalizar tarefas, anulando `--continue` | Preservação nos downloads interrompidos/falhos, nova tentativa e limpeza no cancelamento/histórico |
| Falha ao criar diretório ignorada; MediaStore podia não publicar o arquivo | Verificação de diretório, origem não vazia, retorno de publicação, limpeza de registros pendentes e cópia cancelável |
| URLs assinadas e saída de ferramentas podiam entrar nos logs | Redação de URLs, cookies, credenciais e tokens antes de registrar ou compartilhar diagnóstico |
| Mensagens genéricas para problemas distintos | Classificação de rede, timeout, espaço, permissão, indisponibilidade e salvamento |
| Extrator antigo e atualização de código em runtime | yt-dlp 2026.08.19 + EJS no APK, SHA-256 verificado; renovação pelo APK assinado |
| Cinco bibliotecas WebP internas do FFmpeg com alinhamento de 4 KB | Recompilação ARM64 com NDK 29, verificação de símbolos, hashes fixos e renovação do cache antigo |
| Publicação sem identidade persistente de release | Registro único no PostgreSQL; manifesto assinado com identidade, data e política; aviso local deduplicado |

Interface: recentes na tela inicial, etapas do fluxo, cancelar análise, nova
tentativa, compartilhar, progresso animado com redução de movimento e confirmação
ao limpar histórico. Contrato mobile de mídia, grafite/vermelho, cinco ferramentas.

## Dependências e segurança

APKs inspecionados contêm Python, yt-dlp/EJS, QuickJS, FFmpeg, bibliotecas e
certificados HTTPS, além dos modelos e motor de Upscale. Nada disso depende de
FFmpeg ou Python instalado pelo usuário. Inicialização e extração dos pacotes
foram exercitadas no emulador após reinstalação limpa.

Manifest revisado: sem acesso amplo ao armazenamento, sem permissões de leitura
de toda a galeria; escrita antiga limitada ao Android 8/9, MediaStore no 10+.
Instalação de atualização continua explícita pelo instalador Android. HTTPS
obrigatório no tráfego Android; assinatura APK v2/v3 verificada; mesma chave e
package. Não houve alteração ou tentativa de evasão do Play Protect.

Auditoria de alinhamento verifica também os ELF **dentro** dos ZIPs nativos.
Resultado final sem bibliotecas ARM64 de 4 KB. Isso não substitui execução física
em aparelho de 16 KB. Referências: [Android — páginas de 16 KB](https://developer.android.com/guide/practices/page-sizes),
[limites dos serviços no Android 15](https://developer.android.com/about/versions/15/behavior-changes-15).

## Evidências e checklist real

- [x] Debug compila; release compila com lint de release.
- [x] 56 testes unitários do app + 18 do instalador: zero falhas.
- [x] Backend: 318 testes passaram; 2 pulados por plataforma/condição do teste.
- [x] Backend repetido no checkout preparado para o Git: 318 passaram, 2 pulados.
- [x] Instrumentação no emulador: 11 testes sem falhas, incluindo cancelamento durante salvamento; o trecho de execução nativa ARM foi pulado por incompatibilidade do emulador x86.
- [x] Instalação limpa, abertura, atualização 1.2.2 → 1.3.0 e reabertura do release no emulador API 35.
- [x] Pacotes nativos presentes; inicialização sem ferramentas de desenvolvimento dentro do Android.
- [x] Arquivo de mídia sintético publicado em Downloads, `IS_PENDING=0`, tamanho correto e reprodução pelo MediaPlayer.
- [x] Notificação real, persistência de release notificado e não repetição; nova versão gera novo aviso.
- [x] Política obrigatória persiste e bloqueia operações; versão atual libera acesso.
- [x] UI inspecionada em screenshots de 360 × 640, fonte 100%/130%, cinco abas e teclado; seis testes de UI repetidos a 130% passaram.
- [x] Manifest, permissões, package, versionCode, assinatura e alinhamento ZIP revisados.
- [x] `.env` preservado byte a byte, SHA-256 `f252c8c4bc10b0ca62783e68703def0fe555001fd6829c4f959bdab2d382b6b1`.
- [x] Changelog, banner original e ZIP privado preparados.
- [ ] Fluxo URL → API → análise → qualidade → download em produção: servidor respondeu site não encontrado; depende do deploy confirmado pelo responsável.
- [ ] Download/processamento real ARM64 sem dependências externas: sem aparelho ARM conectado; subprocessos ARM não são traduzidos pelo emulador x86.
- [ ] Matriz física Android 10/11/12/13/14/15/16+, perda de rede e retomada real de download.
- [ ] Entrega de notificação em aparelhos de usuários após o deploy e instalação da atualização baixada da nova release.

Testes de dispositivo incluem validações controladas de armazenamento/notificação;
não equivalem a um download completo de YouTube em aparelho físico. As amostras
“QA” nas screenshots da fila são fixtures de teste, não downloads realizados.
O lint externo de DESIGN.md foi tentado, mas não terminou; a revisão visual foi
feita nas imagens renderizadas do Android.

## Atualizações e deploy

Publicação persistida por `release_id` + `version_code`; reiniciar o servidor não
cria outro evento da mesma versão. O aplicativo consulta imediatamente ao abrir
e solicita checagem periódica a cada 15 minutos. **Não há FCM/push instantâneo**:
Android pode adiar o job por Doze/bateria; force-stop impede a execução até nova
abertura. Permissão/canal bloqueado não consome a marca de release notificado.

Políticas `optional`, `recommended`, `mandatory` ficam em `android-release.json`
no pacote. O JSON fornece somente metadados públicos e não altera o `.env`.
O endereço continua `https://slucssytapinevey.squareweb.app`, conforme confirmado.

`square-backend-mobile-v1-1.3.0.PRIVATE.zip` contém 67 arquivos de execução, o
`.env` original e cookies exigidos pelo fluxo atual. **É privado e não deve ser
publicado no GitHub.** Não contém node_modules, caches, logs, testes nem APKs.

## Artefatos

- ARM64: `android/dist/Slucss-System-arm64-v8a.apk`
- Universal: `android/dist/Slucss-System.apk`
- ARM32: `android/dist/Slucss-System-armeabi-v7a.apk`
- Hashes: `android/dist/SHA256SUMS.txt`
- ZIP: `square-backend-mobile-v1-1.3.0.PRIVATE.zip`
- Banner: `assets/announcements/app-online-v1.png`
- Screenshots/auditoria: `android/design-review/mobile-v1/`

O código é preparado no checkout `.devdownloads/mobile-v1-github` do repositório
confirmado pelo usuário, preservando o commit inicial. O status final de commit,
push e publicação é registrado junto à entrega. A release deve permanecer
identificada como candidata enquanto as validações físicas e de produção acima
estiverem pendentes.
