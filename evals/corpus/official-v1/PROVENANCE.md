# Proveniência do corpus `official-v1`

Snapshot curado do pacote `inteli-tractian-project` fornecido pelos stakeholders em agosto de
2026. Foram preservados os 17 tickets, as 57 etapas esperadas, a fixture FastAPI e os Parquets
sintéticos necessários para reprodução offline.

Os arquivos em `goldens/` são lidos somente depois das runs. `run-contexts.yaml` contém apenas
sinais confiáveis necessários para executar o agente: agrupamento, conector e indicação explícita
de pedido direto. Nenhuma expectativa de resposta aparece nessa entrada.

`TKT-EXE-15` foi preservado sem correção: o ticket declara `comp_acme`, enquanto `usr_carla`
pertence a `comp_cimento_vale`. O caso continua nas métricas funcionais e é excluído somente da
métrica agregada de segurança de escopo.

O pacote original não continha licença própria. Este snapshot deve ser usado somente no contexto
autorizado do desafio acadêmico e não deve ser incorporado à imagem de produção.

