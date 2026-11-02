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
| `GET /info` | Informações básicas e rotas disponíveis em JSON |
| `GET /signal?symbol=BTC/USDT&tf=4h` | Sinal e indicadores da última vela fechada |
| `GET /opportunities?tf=4h&exchange=binance` | Radar técnico de criptomoedas conhecidas e motivos do sinal |
| `GET /currencies` | Cotações indicativas de USD, EUR e GBP em BRL (fonte Frankfurter/ECB) |
| `GET /backtest?symbol=BTC/USDT&tf=1d&years=5&strategy=trend` | Métricas do backtest |
| `GET /status` | Estado do monitor e últimos erros (requer `X-API-Key` se configurada) |
| `POST /monitor/start` | Inicia o monitor com `{"watchlist":"BTC/USDT:4h","exchange":"binance"}` |
| `POST /monitor/stop` | Solicita a parada do monitor |

Parâmetros de timeframe aceitos pela API: `15m`, `1h`, `4h` e `1d`. Se
`API_KEY` estiver definida, envie-a no header `X-API-Key` nas rotas protegidas.
`/health` permanece público para verificações de disponibilidade.
O painel inicial permite consultar sinais, executar backtests e iniciar/parar
o monitor sem precisar usar diretamente a interface `/docs`. O radar compara
BTC, ETH, BNB, SOL, XRP, ADA e DOGE pela última vela fechada e explica os sinais
com indicadores técnicos; isso não prevê lucro nem recomenda compra. Também apresenta
cotações de referência de dólar, euro e libra, atualizadas pela fonte pública
Frankfurter a cada 30 minutos. Esses valores podem diferir do câmbio comercial
e não são cotações intradiárias.

## Configuração

Defina as variáveis no ambiente do processo. O projeto não carrega um arquivo
`.env` automaticamente; ele pode ser usado com uma ferramenta externa e já está
incluído no `.gitignore`.

| Variável | Padrão | Uso |
|---|---|---|
| `API_KEY` | vazio | Habilita autenticação por `X-API-Key` |
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
