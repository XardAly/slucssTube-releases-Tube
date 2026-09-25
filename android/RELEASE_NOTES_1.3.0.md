# Mobile V1 — 1.3.0 (23)

Anterior: 1.2.2 (22). Mesmo pacote `com.xard.ytsystem` e certificado oficial.

- Download interrompido preserva parciais para nova tentativa; cancelamento é
  distinto de falha. Diretórios e publicação no MediaStore são verificados.
- Erros de rede, timeout, armazenamento, permissão e indisponibilidade têm
  mensagens específicas. Diagnóstico remove URLs, cookies, tokens e segredos.
- yt-dlp 2026.08.19 e EJS embarcados com SHA-256; atualização do extrator passa
  pelo APK assinado. Python, certificados, QuickJS e FFmpeg incluídos por ABI.
- Bibliotecas WebP ARM64 internas do FFmpeg recompiladas para alinhamento 16 KB;
  instalação existente renova somente o cache extraído dessas ferramentas.
- Recentes na tela inicial, cancelar análise, tentar novamente, compartilhar,
  progresso animado e confirmação ao limpar histórico.
- Manifesto assinado inclui release_id, published_at e política
  optional/recommended/mandatory. Publicação persistida no PostgreSQL, aviso
  deduplicado no aparelho e bloqueio de operações para política obrigatória.
- JobScheduler verifica o manifesto em intervalos solicitados de 15 minutos e
  a abertura do app verifica imediatamente. **Não é push instantâneo**: Doze,
  restrições de bateria, force-stop e permissão de notificações afetam a entrega.

## Deploy

O ZIP privado contém o `.env` original sem alteração, cookies e
`android-release.json` com metadados públicos da nova build. Esse JSON tem
precedência apenas nas chaves públicas permitidas. Não subir o ZIP ao GitHub.
Os APKs devem estar publicados na tag `android-v1.3.0` antes do deploy.

O endereço confirmado é `https://slucssytapinevey.squareweb.app`. Na auditoria,
a Square Cloud respondeu “site não encontrado”; o responsável precisa subir o
ZIP nesse endereço. Nenhuma implantação na Square Cloud foi executada.

## Limites de homologação

A execução ARM64 física, páginas de 16 KB reais e o fluxo integrado com a API
publicada precisam de homologação após o deploy. O emulador disponível é x86_64
com tradução ARM para bibliotecas; ele não traduz os subprocessos Python/FFmpeg.
Não há promessa de ausência de alertas do Play Protect.
