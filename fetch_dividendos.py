"""
Busca dividendos de ações e FIIs do PlayInvest e grava no Firestore.
Roda como GitHub Action 1x por dia.
"""
import os
import json
import time
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore

# ============ CONFIG ============
PLAYINVEST_URL = "https://playinvest.com.br/dividendos/{ticker}"
DELAY_BETWEEN_REQUESTS = 2  # seconds

# ============ FIREBASE INIT ============
def init_firebase():
    """Initialize Firebase from GitHub Secret (JSON string in env var)."""
    cred_json = os.environ.get("FIREBASE_CREDENTIALS")
    if not cred_json:
        raise ValueError("FIREBASE_CREDENTIALS env var not set")
    
    cred_dict = json.loads(cred_json)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    return firestore.client()

# ============ FETCH FROM PLAYINVEST ============
def fetch_dividendos_playinvest(ticker):
    """Fetch dividend history from PlayInvest for a single ticker."""
    url = PLAYINVEST_URL.format(ticker=ticker.lower())
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"  ✗ {ticker}: HTTP {resp.status_code}")
            return []
        
        soup = BeautifulSoup(resp.text, "html.parser")
        dividends = []
        
        # PlayInvest table: Data Com | R$ | Pagamento | Tipo | x (valor exato)
        rows = soup.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            
            data_com_raw = cells[0].get_text(strip=True)
            valor_raw = cells[1].get_text(strip=True)
            data_pgto_raw = cells[2].get_text(strip=True)
            tipo = cells[3].get_text(strip=True)
            
            # Valor exato (coluna 5 se existir)
            valor_exato = cells[4].get_text(strip=True) if len(cells) > 4 else valor_raw
            
            # Parse dates DD/MM/YYYY -> YYYY-MM-DD
            dc = parse_date_br(data_com_raw)
            dp = parse_date_br(data_pgto_raw)
            
            # Parse value
            try:
                rate = float(valor_exato.replace(",", "."))
            except (ValueError, AttributeError):
                continue
            
            if dc and rate > 0:
                dividends.append({
                    "dc": dc,
                    "dp": dp or dc,
                    "r": rate,
                    "l": tipo
                })
        
        print(f"  ✓ {ticker}: {len(dividends)} eventos")
        return dividends
    
    except Exception as e:
        print(f"  ✗ {ticker}: {e}")
        return []

def parse_date_br(text):
    """Convert DD/MM/YYYY to YYYY-MM-DD."""
    if not text:
        return None
    parts = text.strip().split("/")
    if len(parts) == 3:
        try:
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
        except:
            return None
    return None

# ============ CALCULATE DY WITH SHARES AT DATE ============
def shares_at(operations, date):
    """Calculate how many shares were held at a given date."""
    total = 0
    for op in operations:
        if op.get("d", "") <= date:
            total += op.get("q", 0)
    return max(0, total)

def process_dividends(all_div_events, operations):
    """
    For each dividend event, calculate actual value received
    based on shares held at data com.
    Returns: (dy_known, monthly, filtered_events)
    """
    dy_known = {}
    monthly = {}
    filtered_events = {}
    
    for ticker, events in all_div_events.items():
        ops = operations.get(ticker, [])
        relevant = []
        ticker_total = 0
        
        for ev in events:
            cotas = shares_at(ops, ev["dc"])
            if cotas <= 0:
                continue  # didn't own shares at data com
            
            valor = ev["r"] * cotas
            relevant.append(ev)
            ticker_total += valor
            
            month = (ev.get("dp") or ev["dc"])[:7]
            monthly[month] = monthly.get(month, 0) + valor
        
        if relevant:
            filtered_events[ticker] = relevant
        if ticker_total > 0:
            dy_known[ticker] = round(ticker_total, 2)
    
    # Build monthly list, filter zeros
    monthly_list = [
        {"data": m, "total": round(v, 2)}
        for m, v in sorted(monthly.items())
        if v > 0.01
    ]
    
    return dy_known, monthly_list, filtered_events

# ============ MAIN ============
def main():
    print("=" * 50)
    print(f"Fetch Dividendos — {datetime.now().isoformat()}")
    print("=" * 50)
    
    # Init Firebase
    db = init_firebase()
    
    # Load ativos from Firestore
    ativos_doc = db.collection("investimentos").document("ativos").get()
    if not ativos_doc.exists:
        print("Nenhum ativo encontrado no Firestore. Importe primeiro.")
        return
    
    ativos = ativos_doc.to_dict().get("lista", [])
    carteira = [a for a in ativos if a.get("s") == "C"]
    tickers = [a["k"] for a in carteira if a.get("tp") != "CRIPTO"]
    
    print(f"Tickers na carteira: {len(tickers)}")
    print(f"Tickers: {', '.join(tickers)}")
    
    # Load operations from Firestore
    ops_doc = db.collection("investimentos").document("operacoes").get()
    operations = ops_doc.to_dict() if ops_doc.exists else {}
    
    # Load existing dividend data
    div_doc = db.collection("investimentos").document("dividendos").get()
    existing = div_doc.to_dict() if div_doc.exists else {}
    existing_dy = existing.get("dy_known", {})
    
    # Fetch dividends from PlayInvest
    print("\n--- Buscando PlayInvest ---")
    all_events = {}
    success = 0
    fail = 0
    
    for i, ticker in enumerate(tickers):
        print(f"[{i+1}/{len(tickers)}]", end="")
        events = fetch_dividendos_playinvest(ticker)
        if events:
            all_events[ticker] = events
            success += 1
        else:
            fail += 1
        time.sleep(DELAY_BETWEEN_REQUESTS)
    
    print(f"\nResultado: {success} OK, {fail} falhas")
    
    # Process: calculate DY using sharesAt
    print("\n--- Calculando DY ---")
    dy_known, monthly, filtered = process_dividends(all_events, operations)
    
    # Merge with existing DY (keep existing for tickers that failed fetch)
    for tk, val in existing_dy.items():
        if tk not in dy_known:
            dy_known[tk] = val
    
    print(f"DY calculado para {len(dy_known)} tickers")
    total_dy = sum(dy_known.values())
    print(f"Total DY: R$ {total_dy:.2f}")
    print(f"Meses com dados: {len(monthly)}")
    
    # Save to Firestore
    print("\n--- Salvando no Firestore ---")
    db.collection("investimentos").document("dividendos").set({
        "dy_known": dy_known,
        "mensal": monthly,
        "eventos": filtered,
        "ultimaAtualizacao": datetime.now().isoformat(),
        "fonte": "PlayInvest (GitHub Action)"
    })
    
    print("✓ Salvo com sucesso!")
    
    # Summary
    print("\n--- Resumo ---")
    for tk in sorted(dy_known.keys()):
        n_events = len(filtered.get(tk, []))
        print(f"  {tk}: R$ {dy_known[tk]:.2f} ({n_events} eventos)")

if __name__ == "__main__":
    main()
