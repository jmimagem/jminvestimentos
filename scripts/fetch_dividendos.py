"""
Busca dividendos:
  - Ações: PlayInvest (HTML scraping)
  - FIIs: Yahoo Finance via yfinance (API JSON)
Grava no Firestore. Roda como GitHub Action 1x por dia.
"""
import os
import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore
import yfinance as yf

DELAY = 2
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ============ FIREBASE ============
def init_firebase():
    cred = credentials.Certificate(json.loads(os.environ["FIREBASE_CREDENTIALS"]))
    firebase_admin.initialize_app(cred)
    return firestore.client()

# ============ PLAYINVEST (AÇÕES) ============
def fetch_playinvest(ticker):
    url = f"https://playinvest.com.br/dividendos/{ticker.lower()}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        divs = []
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            dc = parse_date_br(cells[0].get_text(strip=True))
            dp = parse_date_br(cells[2].get_text(strip=True))
            tipo = cells[3].get_text(strip=True)
            val_text = cells[4].get_text(strip=True) if len(cells) > 4 else cells[1].get_text(strip=True)
            try:
                rate = float(val_text.replace(",", "."))
            except:
                continue
            if dc and rate > 0:
                divs.append({"dc": dc, "dp": dp or dc, "r": rate, "l": tipo})
        return divs
    except Exception as e:
        print(f"    PlayInvest erro: {e}")
        return []

# ============ YAHOO FINANCE (FIIs) ============
def fetch_yahoo_fii(ticker):
    """Busca dividendos de FIIs via yfinance."""
    try:
        tk = yf.Ticker(f"{ticker}.SA")
        divs_df = tk.dividends
        if divs_df is None or divs_df.empty:
            return []
        
        result = []
        for date, amount in divs_df.items():
            if amount > 0:
                # yfinance retorna a ex-date como index
                dc = date.strftime("%Y-%m-%d")
                result.append({
                    "dc": dc,
                    "dp": dc,  # Yahoo não dá data de pagamento separada
                    "r": round(float(amount), 6),
                    "l": "RENDIMENTO"
                })
        return result
    except Exception as e:
        print(f"    Yahoo erro: {e}")
        return []

def parse_date_br(text):
    if not text:
        return None
    parts = text.strip().split("/")
    if len(parts) == 3:
        try:
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
        except:
            return None
    return None

# ============ CALCULATIONS ============
def shares_at(operations, date):
    total = 0
    for op in operations:
        if op.get("d", "") <= date:
            total += op.get("q", 0)
    return max(0, total)

def process_dividends(all_events, operations):
    dy_known = {}
    monthly = {}
    filtered = {}
    
    for tk, events in all_events.items():
        ops = operations.get(tk, [])
        relevant = []
        ticker_total = 0
        
        for ev in events:
            cotas = shares_at(ops, ev["dc"])
            if cotas <= 0:
                continue
            valor = ev["r"] * cotas
            relevant.append(ev)
            ticker_total += valor
            month = (ev.get("dp") or ev["dc"])[:7]
            monthly[month] = monthly.get(month, 0) + valor
        
        if relevant:
            filtered[tk] = relevant
        if ticker_total > 0:
            dy_known[tk] = round(ticker_total, 2)
    
    monthly_list = [
        {"data": m, "total": round(v, 2)}
        for m, v in sorted(monthly.items())
        if v > 0.01
    ]
    return dy_known, monthly_list, filtered

# ============ MAIN ============
def main():
    print("=" * 60)
    print(f"Fetch Dividendos — {datetime.now().isoformat()}")
    print("=" * 60)
    
    db = init_firebase()
    
    ativos = db.collection("investimentos").document("ativos").get()
    if not ativos.exists:
        print("Nenhum ativo."); return
    
    lista = ativos.to_dict().get("lista", [])
    carteira = [a for a in lista if a.get("s") == "C"]
    
    acoes = [a["k"] for a in carteira if a.get("tp") == "AÇÕES"]
    fiis = [a["k"] for a in carteira if a.get("tp") == "FII"]
    
    print(f"Ações: {len(acoes)} (PlayInvest)")
    print(f"FIIs:  {len(fiis)} (Yahoo Finance)")
    
    ops_doc = db.collection("investimentos").document("operacoes").get()
    operations = ops_doc.to_dict() if ops_doc.exists else {}
    
    div_doc = db.collection("investimentos").document("dividendos").get()
    existing_dy = div_doc.to_dict().get("dy_known", {}) if div_doc.exists else {}
    
    all_events = {}
    
    # --- AÇÕES ---
    print(f"\n{'='*40}")
    print("AÇÕES — PlayInvest")
    print(f"{'='*40}")
    ok_a = 0
    for i, tk in enumerate(acoes):
        print(f"[{i+1}/{len(acoes)}] {tk}...", end="", flush=True)
        ev = fetch_playinvest(tk)
        if ev:
            all_events[tk] = ev
            ok_a += 1
            print(f" ✓ {len(ev)} eventos")
        else:
            print(f" ✗")
        time.sleep(DELAY)
    
    # --- FIIs ---
    print(f"\n{'='*40}")
    print("FIIs — Yahoo Finance")
    print(f"{'='*40}")
    ok_f = 0
    for i, tk in enumerate(fiis):
        print(f"[{i+1}/{len(fiis)}] {tk}...", end="", flush=True)
        ev = fetch_yahoo_fii(tk)
        if ev:
            all_events[tk] = ev
            ok_f += 1
            print(f" ✓ {len(ev)} eventos")
        else:
            print(f" ✗")
        time.sleep(1)
    
    print(f"\nFetch: Ações {ok_a}/{len(acoes)}, FIIs {ok_f}/{len(fiis)}")
    
    # --- Process ---
    print(f"\n{'='*40}")
    print("Calculando DY × sharesAt()...")
    print(f"{'='*40}")
    dy_known, monthly, filtered = process_dividends(all_events, operations)
    
    # Keep existing for failed tickers
    for tk, val in existing_dy.items():
        if tk not in dy_known:
            dy_known[tk] = val
    
    total = sum(dy_known.values())
    print(f"Total DY: R$ {total:.2f} ({len(dy_known)} tickers, {len(monthly)} meses)")
    
    # --- Save ---
    db.collection("investimentos").document("dividendos").set({
        "dy_known": dy_known,
        "mensal": monthly,
        "eventos": filtered,
        "ultimaAtualizacao": datetime.now().isoformat(),
        "fonte": "PlayInvest (ações) + Yahoo Finance (FIIs)"
    })
    print("✓ Firestore salvo!")
    
    # --- Detail ---
    print(f"\n{'='*40}")
    for tk in sorted(dy_known.keys()):
        n = len(filtered.get(tk, []))
        src = "PI" if tk in acoes else "YF" if tk in fiis else "?"
        print(f"  {tk}: R$ {dy_known[tk]:.2f} ({n} ev) [{src}]")

if __name__ == "__main__":
    main()
