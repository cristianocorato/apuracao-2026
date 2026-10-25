# Apuração do 2º turno para presidente (2026)

Site com os resultados oficiais do TSE, atualizado automaticamente, que compara o
1º e o 2º turno no Brasil, em cada estado e em cada cidade:

- placar nacional e quanto já foi apurado;
- mapa do Brasil por estado: vermelho onde Lula está à frente, verde onde Flávio
  Bolsonaro está à frente (tons mais fortes = vantagem maior);
- tabela por estado: resultado, mudança desde o 1º turno e contagem de cidades;
- busca por cidade com o comparativo 1º turno × 2º turno de cada candidato;
- as 10 cidades onde cada um mais ganhou e mais perdeu votos (no Brasil, em cada
  estado ou no exterior), em votos ou em pontos percentuais.

## Como funciona

| Arquivo | Para que serve |
|---|---|
| `index.html` | O site inteiro (HTML, CSS e JavaScript; só a fonte vem do Google Fonts) |
| `scripts/coletar.mjs` | Robô que baixa os arquivos oficiais do TSE e grava em `dados/` |
| `.github/workflows/atualizar-dados.yml` | Roda o robô no GitHub e publica o site |
| `dados/` | Criada pelo robô na primeira rodada |

O robô usa os arquivos públicos de divulgação do TSE (`resultados.tse.jus.br`,
eleições 6257 e 6258). Ele respeita o limite do TSE (no máximo 100 requisições
por segundo; usamos 30), só pede arquivos que existem e, a cada rodada, baixa de
novo apenas as cidades cuja apuração mudou. Se o TSE bloquear, ele espera 12
minutos antes de tentar de novo.

O placar nacional e os estados também são lidos direto do TSE pelo navegador de
quem visita, a cada minuto, quando o TSE permite. Se não permitir, o site usa os
dados do robô.

## Colocar no ar com o GitHub Pages

1. Crie um repositório **público** no GitHub (por exemplo, `apuracao-2026`).
2. Envie todos os arquivos desta pasta, mantendo as pastas — inclusive a
   `.github`, que é onde fica o robô.
3. No repositório, abra **Settings → Pages** e, em **Build and deployment →
   Source**, escolha **GitHub Actions**.
4. Abra a aba **Actions**, clique em **Atualizar dados do TSE** e depois em
   **Run workflow**.
5. A primeira rodada leva uns 5 minutos, porque baixa o 1º turno de todas as
   cidades. Depois disso o site fica em
   `https://SEU-USUARIO.github.io/NOME-DO-REPOSITORIO/`.

A partir daí o robô roda sozinho a cada 5 minutos. Como o agendamento do GitHub
costuma atrasar nos horários de pico, durante a apuração cada rodada já pede a
próxima (o período fica em `ENCADEAR_DE` e `ENCADEAR_ATE`, no arquivo do robô):
os dados das cidades se renovam a cada 2 ou 3 minutos. Isso para sozinho quando
o TSE publica a totalização final do 2º turno. A página confere se há novidade a
cada minuto. Quando nada muda no TSE, o robô não grava nem publica nada.

Se o robô parecer parado, abra **Actions → Atualizar dados do TSE → Run
workflow**: a rodada manual também retoma a sequência.

**Depois que a apuração terminar**, desligue o robô: Actions → Atualizar dados do
TSE → menu `···` → **Disable workflow**. O site continua no ar com o resultado final.

## Rodar no seu computador

Precisa do Node 18 ou mais novo.

```bash
node scripts/coletar.mjs        # baixa os dados para a pasta dados/
python3 -m http.server 8000     # depois abra http://localhost:8000
```

Ajustes opcionais (variáveis de ambiente do robô):

| Variável | Padrão | O que faz |
|---|---|---|
| `TSE_ELEICAO_T1` / `TSE_ELEICAO_T2` | `6257` / `6258` | Códigos das eleições no TSE |
| `REQ_POR_SEGUNDO` | `30` | Ritmo máximo de requisições ao TSE |
| `TEMPO_MAXIMO_MIN` | `9` | Tempo máximo de cada rodada; o que faltar fica para a próxima |
| `PASTA_DADOS` | `dados` | Onde gravar os arquivos |

## Como os números são calculados

- Percentuais sobre os votos válidos, como faz o TSE.
- Comparação por cidade: votos do 2º turno menos votos do 1º turno. Enquanto uma
  cidade ainda está em apuração, essa diferença fica distorcida, por isso os
  rankings usam, por padrão, só cidades com apuração concluída.
- O resultado oficial é sempre o da totalização final do TSE.

## Créditos

- Dados: Tribunal Superior Eleitoral.
- Mapa: malha estadual do IBGE, via projeto click_that_hood (licença MIT).
- Fonte: Archivo, The Archivo Project Authors (SIL Open Font License 1.1), servida pelo Google Fonts.

Site independente, sem ligação com o TSE, partidos ou campanhas.
