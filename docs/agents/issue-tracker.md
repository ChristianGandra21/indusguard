# Issue tracker: GitHub

Issues e especificações deste repositório são armazenadas no GitHub Issues de
`ChristianGandra21/indusguard`. Use o CLI `gh` para todas as operações.

## Convenções

- Criar issue: `gh issue create`.
- Ler issue: `gh issue view <number> --comments`.
- Listar issues: `gh issue list`.
- Comentar: `gh issue comment <number>`.
- Adicionar ou remover labels: `gh issue edit`.
- Fechar issue: `gh issue close`.

O repositório deve ser inferido pelo remote Git quando os comandos forem executados dentro do
clone.

## Pull requests como superfície de triagem

Pull requests não são tratados como solicitações ou issues para triagem.

## Publicação pelas skills

Quando uma skill solicitar publicação no issue tracker, deve ser criada uma issue no GitHub.

Quando uma skill solicitar o ticket relevante, deve ser usado
`gh issue view <number> --comments`.
