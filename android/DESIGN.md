---
version: alpha
name: Slucss Media Workbench
colors:
  primary: "#CF243B"
  background: "#101012"
  input: "#19191C"
  text: "#F2F2F3"
  secondary: "#B9B9BF"
  border: "#36363C"
typography:
  body-md:
    fontFamily: Roboto
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.5
rounded:
  sm: 12px
spacing:
  md: 16px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "#ffffff"
  input:
    backgroundColor: "{colors.input}"
    textColor: "{colors.text}"
  divider:
    backgroundColor: "{colors.border}"
---

## Overview

Slucss é uma bancada mobile de mídia: baixar vídeo/áudio, criar GIF, converter e ampliar com IA. Interface Android nativa, com a densidade de ferramentas de criação e o cuidado tipográfico de aplicativos de mídia. A revisão de setembro de 2026 substitui a composição orbital rejeitada pelo usuário por uma área de mídia contida e ferramentas com hierarquia consistente.

## Colors

Grafite neutro contínuo. Vermelho destaca a ação principal e a seleção. O indicador da navegação usa vermelho translúcido; texto selecionado usa #FF7185. Campos usam #19191C, bordas #36363C. Não há gradientes, brilho ou sombras nos controles. Reflexos pertencem à mídia.

## Typography

Roboto nativa. Títulos de ferramenta em 32sp, peso bold, tracking -0.025; subtítulos em 14sp. Campos e botões principais em 16sp, rótulos em 14sp, informações auxiliares em 12sp. Navegação em 11sp com rótulo persistente e peso maior na seleção. Textos respeitam a escala de fonte do Android.

## Layout

Gutter de 20dp, barra superior de 64dp e navegação inferior de 80dp, além das áreas seguras. Topo de conteúdo de 16dp em telas pequenas e 24dp a partir de 700dp de altura útil. As cinco abas são fixas; criação nas ferramentas dedicadas e acompanhamento na Fila. Não duplicar destinos em atalhos na tela inicial.

Baixar contém título, instrução curta, mídia, campo de link com Colar e uma ação principal. A mídia mede 128dp em telas pequenas e 184dp a partir de 700dp de altura útil. Link e Buscar vídeo devem aparecer sem rolagem em 360 × 640dp na escala de fonte padrão. Nas outras abas, a escolha do arquivo precede os parâmetros. Filtros da fila se reorganizam em linhas. O teclado oculta a navegação e o conteúdo continua rolável.

## Elevation & Depth

Sem sombras de cartões. Uma borda discreta delimita a mídia. A área de escolha de arquivo tem superfície e contorno porque é um controle interativo. Saída da conversão e itens da fila usam divisórias. Evitar caixas decorativas ou seções inteiras dentro de cartões.

## Shapes

Campos, seletores, miniaturas e botões usam raio de 12dp. Indicador da navegação e filtro selecionado usam 10dp. Área da mídia usa 16dp para conter o vídeo. Ícones da navegação compartilham viewport 24 e traço de 1.8.

## Components

- Formulários nativos com entrada, seleção, desabilitado, erro, carregamento e resultado.
- Seleção de arquivo em área de 104dp com ícone e texto; nome do arquivo imediatamente abaixo.
- Ações de tarefa: cancelar, abrir, compartilhar, tentar novamente, ver erro e tentar servidor. Progresso animado respeita redução de movimento.
- Download mostra três tarefas recentes com ações reais; a Fila mantém histórico e filtros. Passos Link → Qualidade → Download orientam o fluxo. Análise pode ser cancelada sem perder o link.
- Limpar histórico pede confirmação e mantém arquivos publicados. Atualização obrigatória bloqueia novas tarefas também no serviço.
- Fila vazia com uma frase e ação; filtros com seleção visível além da cor do texto.
- StudioHeroView reproduz o arquivo local assets/studio/hero.mp4, silencioso e em loop, quando presente. Mantém o pôster até o primeiro frame e em falha. Sem vídeo, mostra somente o pôster e oculta o controle de movimento.
- Pausa manual persistente. Decodificador liberado fora da tela, sem foco e ao destacar a view; reprodução pausada com animações do sistema desativadas ou economia de bateria. Nenhum download remoto em runtime para a arte.
- O vídeo solicitado deve ser gerado pelo Higgsfield. Enquanto a autenticação do plugin estiver indisponível, não apresentar o pôster como vídeo gerado.

## Do's and Don'ts

Preservar as cinco ferramentas e seus fluxos reais. Sem slogans, métricas artificiais, órbitas desenhadas por código ou textos decorativos. Não levar implementação para os textos do aplicativo. Verificar as cinco telas no emulador Android, incluindo tela pequena, fonte ampliada e teclado. Distinguir validação visual de teste de processamento de mídia real.
