# Crypto Signals

Ferramenta de apoio para acompanhar sinais técnicos de criptomoedas. O projeto
consulta candles públicos de corretoras via CCXT, calcula indicadores e expõe
os resultados por linha de comando e API. **Não executa ordens e não é
recomendação de investimento.**

## Requisitos

- Python 3.11 ou superior
- Acesso à internet para consultar os dados públicos da corretora

## Instalação

No PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Linha de comando

```powershell
# Sinal mais recente, calculado apenas com candles fechados
python crypto_signals.py now --symbol BTC/USDT --tf 4h

# Backtest com as últimas 1000 velas
python crypto_signals.py backtest --symbol BTC/USDT --tf 1d --strategy trend

# Backtest com até 5 anos de histórico e o período final reservado para teste
python crypto_signals.py backtest --symbol BTC/USDT --tf 1d --years 5 --strategy score --split 0.7

# Monitoramento contínuo no terminal
python crypto_signals.py live --symbol BTC/USDT --tf 1h --poll 60
```

Modos disponíveis: `now`, `backtest` e `live`. Estratégias: `trend` (segue a
tendência de longo prazo) e `score` (usa a pontuação dos indicadores). O
argumento `--exchange` aceita um identificador de exchange suportado pelo
CCXT, como `binance`, `kraken`, `bybit`, `okx` ou `coinbase`.
Na API e no monitor, se uma corretora retornar erro de restrição geográfica,
o sistema tenta automaticamente as outras corretoras suportadas e informa qual
foi usada no resultado.

## API

Inicie o servidor local:

```powershell
uvicorn api:app --reload
```

Documentação interativa: <http://127.0.0.1:8000/docs>.

| Rota | Descrição |
|---|---|
| `GET /health` | Verificação de saúde, sem autenticação |
| `GET /` | Página inicial com a arte do projeto |
| `GET /gestor` | Tela de acesso do gestor; login protegido por `ADMIN_PASSWORD` |
| `POST /admin/login` | Inicia uma sessão administrativa |
| `GET /admin/accesses` | Lista até 500 visitas recentes (requer sessão administrativa) |
| `POST /admin/logout` | Encerra a sessão administrativa |
| `GET /info` | Informações básicas e rotas disponíveis em JSON |
| `GET /signal?symbol=BTC/USDT&tf=4h` | Sinal e indicadores da última vela fechada, sem chave |
| `GET /opportunities?tf=4h&exchange=binance` | Radar técnico de criptomoedas conhecidas e motivos do sinal, sem chave |
| `GET /currencies` | Cotações indicativas de USD, EUR e GBP em BRL (fonte Frankfurter/ECB) |
| `GET /macro` | Meta Selic, Brent, USD/BRL, série histórica e indicador heurístico de pressão nos combustíveis |
| `GET /market-history?symbol=BTC/USDT&tf=1d&exchange=binance` | Histórico de preços para gráficos, usando candles fechados |
| `GET /backtest?symbol=BTC/USDT&tf=1d&years=5&strategy=trend` | Métricas do backtest, sem chave |
| `GET /status` | Estado do monitor e últimos erros (requer `X-API-Key` se configurada) |
| `POST /monitor/start` | Inicia o monitor com `{"watchlist":"BTC/USDT:4h","exchange":"binance"}` (requer chave se configurada) |
| `POST /monitor/stop` | Solicita a parada do monitor (requer chave se configurada) |

Parâmetros de timeframe aceitos pela API: `15m`, `1h`, `4h` e `1d`. As consultas
de sinais, radar, câmbio e backtest são públicas. Se `API_KEY` estiver definida,
informe-a no painel Bot monitor para consultar o estado e controlar o monitor;
as rotas protegidas também aceitam o header `X-API-Key`.
`/health` permanece público para verificações de disponibilidade.
O painel inicial permite consultar sinais, executar backtests e iniciar/parar
o monitor sem precisar usar diretamente a interface `/docs`. O radar compara
BTC, ETH, BNB, SOL, XRP, ADA e DOGE pela última vela fechada e explica os sinais
com indicadores técnicos; isso não prevê lucro nem recomenda compra. Também apresenta
cotações de referência de dólar, euro e libra, atualizadas pela fonte pública
Frankfurter a cada 30 minutos. Esses valores podem diferir do câmbio comercial
e não são cotações intradiárias. A visão geral também apresenta a meta Selic do
Copom, o Brent e o USD/BRL, além de gráficos históricos. O alerta de combustíveis
indica possível pressão quando o Brent convertido em reais sobe pelo menos 5%
em cerca de 30 dias; é apenas uma referência externa, não uma previsão de
reajuste nos postos. Os indicadores macro são atualizados automaticamente a
cada 30 minutos e os gráficos de criptoativos a cada 5 minutos.

Mantenha o logo do painel pressionado por cerca de 1,4 segundo para abrir a tela
do gestor. Esse atalho é apenas discreto: a senha `ADMIN_PASSWORD` e a sessão
validada no servidor são as proteções reais. Configure uma senha forte e única
nas variáveis secretas do ambiente. A sessão expira após 12 horas.

A tela registra horário UTC e o IP visto pelo servidor quando a página inicial
é aberta e mantém no máximo os 500 registros mais recentes. Não coleta nomes nem
localização regional; um IP não identifica por si só uma pessoa, e a região
exigiria uma fonte de geolocalização separada.
Os registros ficam em SQLite no caminho definido por `ACCESS_LOG_DB` (por
padrão, `access_log.sqlite3` na pasta do projeto). Em hospedagens com disco
efêmero, configure essa variável para um disco persistente se precisar manter
o histórico após reinicializações ou novos deploys. Informe os usuários sobre
esse registro e defina um prazo adequado de retenção.

## Configuração

Defina as variáveis no ambiente do processo. O projeto não carrega um arquivo
`.env` automaticamente; ele pode ser usado com uma ferramenta externa e já está
incluído no `.gitignore`.

| Variável | Padrão | Uso |
|---|---|---|
| `API_KEY` | vazio | Habilita autenticação por `X-API-Key` |
| `ADMIN_PASSWORD` | vazio | Habilita o login da tela de gestão; configure um segredo forte |
| `ACCESS_LOG_DB` | `access_log.sqlite3` | Caminho do banco SQLite de visitas |
| `EXCHANGE` | `binance` | Corretora usada pela API e pelo monitor |
| `ENABLE_MONITOR` | `0` | Define `1` para ativar o monitor em segundo plano |
| `WATCHLIST` | `BTC/USDT:4h` | Ativos separados por vírgula, no formato `PAR:timeframe` |
| `POLL_SECONDS` | `300` | Intervalo de consulta do monitor (mínimo 60 segundos) |
| `CACHE_TTL` | `60` | Validade do cache de candles, em segundos |
| `TELEGRAM_TOKEN` | vazio | Token do bot para alertas opcionais |
| `TELEGRAM_CHAT_ID` | vazio | Chat que recebe alertas opcionais |

Para habilitar alertas Telegram, configure `TELEGRAM_TOKEN` e
`TELEGRAM_CHAT_ID`. Sem ambos, as mensagens são exibidas somente no log.

## Testes

```powershell
python -m unittest discover -s tests -v
```

## Deploy no Render

O arquivo `render.yaml` contém o Blueprint do serviço. No Render, crie um
Blueprint a partir deste repositório e informe os segredos do Telegram quando
solicitado. O serviço gera uma `API_KEY` automaticamente. Use uma única
instância se o monitor estiver habilitado para evitar alertas duplicados.

## Indicadores e limites

O sinal combina EMA 50/200, RSI, MACD e volatilidade baseada em ATR. As
classificações são `COMPRAR`, `AGUARDAR` e `EVITAR`; a referência de stop exibida
é apenas informativa. O backtest aplica uma taxa fixa padrão de 0,1% por
mudança de posição, não modela slippage, liquidez, impostos ou execução real,
e resultados históricos não predizem resultados futuros. Revise os parâmetros
e valide a estratégia antes de qualquer decisão financeira.
