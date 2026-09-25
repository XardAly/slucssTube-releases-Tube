# Validação 1.3.1 — imagem no aviso do aplicativo

O banner original agora está no diálogo de atualização e no aviso de conexão
confirmada após instalar esta versão. Este último só aparece após a resposta
assinada do servidor anunciar a versão instalada; a exibição é persistida por
versionCode. Erro de rede não abre o aviso. A imagem é um recurso local do APK.

- 56 testes unitários do app e 18 do instalador, sem falhas.
- Debug e release compilados; assinatura oficial conferida. Atualização do APK
  release para 1.3.1 (24) instalada e aberta no emulador.
- Três testes instrumentados executados a 100% e novamente a 130% de fonte,
  em tela 360 × 640 dp/API 35: seis execuções aprovadas.
- Conferidos: imagem inteira, ações visíveis, callback de download, aviso online
  sem repetição após recriar a Activity e bloqueio do botão Voltar em atualização
  obrigatória. O rótulo Baixar evita corte dos botões com fonte ampliada.
- Screenshots reais: `design-review/mobile-v131/font100/` e `font130/`.
  A versão 1.3.2 exibida no teste de atualização é uma fixture, não uma release.
- A checagem visual usa os diálogos reais, acionados por instrumentação; não
  representa um novo teste de download completo ou da API em produção.

Use o APK 1.3.1 e o ZIP privado de backend 1.3.1 juntos. O ZIP mantém o novo
Supabase configurado anteriormente, sem alterações adicionais em DATABASE_URL
ou nas demais variáveis do `.env`. A versão permanece candidata enquanto a
homologação de download completo em ARM64 físico não for realizada.
