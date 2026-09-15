# Kalshi auth config examples

Both `kalshi_sports_trader.py` and `kalshi_broadcast_word_trader.py` load credentials from:

```text
~/.config/kalshi-multiplex-orderbook/prod.env
~/.config/kalshi-multiplex-orderbook/demo.env
```

Setup:

```bash
mkdir -p ~/.config/kalshi-multiplex-orderbook
chmod 700 ~/.config/kalshi-multiplex-orderbook
cp examples/config/prod.env.example ~/.config/kalshi-multiplex-orderbook/prod.env
cp /path/to/your-prod-private-key.pem ~/.config/kalshi-multiplex-orderbook/prod.private-key.pem
chmod 600 ~/.config/kalshi-multiplex-orderbook/prod.env
chmod 600 ~/.config/kalshi-multiplex-orderbook/prod.private-key.pem
# edit prod.env and set KALSHI_PROD_API_KEY_ID
```

Resolution order:

1. CLI overrides (`--private-key-file`, `--api-key-id-env`, …)
2. Process environment variables
3. `~/.config/kalshi-multiplex-orderbook/{prod,demo}.env`
4. Split files `{prod,demo}.api-key-id` + `{prod,demo}.private-key.pem`
5. Legacy prod-only `./grimm.txt` private key fallback

Never commit real key ids or PEM files.
