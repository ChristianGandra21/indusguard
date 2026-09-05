# Melhoria supervisionada do agente

O ciclo parte de uma avaliação externa concluída e elegível, prepara uma alteração em uma branch
com worktree isolada, executa validações locais e aguarda revisão humana. Somente a confirmação
explícita do diff no terminal cria um commit nessa branch. O ciclo não abre PR, não faz push e não
incorpora mudanças à branch principal.

## O que este MVP executa

O comando reutiliza `EvaluationAnalyzer`: avaliações inválidas, parciais, fake, incompletas ou com
artefatos incompatíveis são recusadas antes da preparação. A origem é sempre o commit registrado
na avaliação; mudanças ainda não commitadas no checkout do operador não entram no candidato.

A seleção de ações é determinística e usa a receita existente `agent-guidance-recipe`, que reforça
o uso de `analysisId` observado em evidências no domínio Tractian. Ela não gera código livre com
LLM. A receita que altera o contraste de `prompt_only` não participa deste fluxo. Se a orientação
já estiver aplicada ou nenhuma categoria corresponder à receita, o estado será `no_changes` e não
haverá commit. Novas receitas exigem implementação e revisão próprias.

## Preparar, validar e revisar

No checkout com as dependências instaladas, use a mesma configuração de banco dos evals:

```bash
.venv/bin/indusguard-eval improvement-prepare EVALUATION_UUID
.venv/bin/indusguard-eval improvement-validate PROPOSAL_UUID
.venv/bin/indusguard-eval improvement-review PROPOSAL_UUID
```

O primeiro comando retorna o UUID da proposta. Os comandos aceitam `--improvements-dir CAMINHO`;
o default é `INDUSGUARD_IMPROVEMENTS_DIR` ou `.data/improvements`. Prefira um caminho absoluto
compartilhado entre CLI e API. O volume deve ser privado e persistente. Os arquivos contêm o plano,
o diff e logs locais; não devem ser publicados ou versionados.

`improvement-validate` executa a validação do corpus e as suítes locais de evals e API sobre o código
do worktree, com o interpretador do operador. Os testes marcados `live` e `postgres` ficam fora.
Credenciais de provedores não são repassadas ao subprocesso e não se inicia piloto externo.
Falhas ou timeout bloqueiam a revisão. O log fica em `PROPOSAL_UUID/validation.log`.

`improvement-review` exige terminal interativo, mostra a base, a branch e o diff completo e solicita
que a pessoa digite o SHA-256 desse diff. `rejeitar` encerra a proposta como rejeitada; qualquer outra
entrada cancela sem aprovar. Mudanças na base, branch, índice, diff ou arquivos não rastreados
invalidam a promoção. A identidade registrada é a identidade Git do operador local; não é uma
assinatura criptográfica nem um login humano comprovado pelo sistema. A fronteira de confiança é o
acesso local ao terminal, ao repositório e ao volume, que não devem ser expostos ao agente avaliado.

O commit é criado a partir da árvore revisada e atualiza somente a branch `improvement/UUID` por
compare-and-swap. Se houver interrupção depois de persistir a aprovação, finalize com:

```bash
.venv/bin/indusguard-eval improvement-recover PROPOSAL_UUID
```

A recuperação reutiliza o SHA já registrado e não cria outra aprovação. Uma proposta em
`validating` após interrupção pode ser validada novamente. Uma preparação interrompida permanece
visível como `preparing`; inspecione seus artefatos e prepare uma nova proposta. Worktrees e branches
não são apagadas automaticamente.

## Visibilidade administrativa

Configure `INDUSGUARD_ADMIN_TOKEN` no ambiente da API com um segredo de pelo menos 32 caracteres.
Ele é separado de `INDUSGUARD_OWNER_TOKEN`. Sem configuração, o endpoint administrativo recusa
acesso. Configure `INDUSGUARD_IMPROVEMENTS_DIR` para o mesmo volume usado pelo CLI; na API ele pode
ser montado somente para leitura. Uma instância remota sem esse volume não verá as propostas locais.
Use HTTPS ao acessar uma API remota.

A página **Melhorias** (`/improvements`) recebe o token em um campo de senha e o mantém somente na
memória da página. A consulta `GET /api/v1/admin/improvements` usa Bearer e `Cache-Control: no-store`.
O admin acompanha estado, avaliação de origem, arquivos propostos, digest, validação, identidade da
aprovação e SHA resultante. Plano, diff, caminhos internos e logs não atravessam o endpoint. A página
não aprova commits; a aprovação ocorre no terminal. Limpar sessão remove o token e os resultados.

## Limite da evidência

Validação local aprovada significa que os checks locais passaram. Não comprova melhoria de
qualidade, segurança ou utilidade em um provedor real. Após revisar e incorporar o commit, um novo
piloto externo exige novo preflight e consentimento explícito. Goldens permanecem preservados.
Não há comparação antes/depois automática neste MVP, nem publicação automática do candidato.
