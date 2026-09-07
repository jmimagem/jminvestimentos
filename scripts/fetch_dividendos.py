"""
Busca dividendos de ações (PlayInvest) e FIIs (StatusInvest) e grava no Firestore.
Roda como GitHub Action 1x por dia.
"""
import os
import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore

# ============ CONFIG ============
DELAY = 2  # seconds between requests
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ============ FIREBASE INIT ============
def init_firebase():
    cred_json = os.environ.get("FIREBASE_CREDENTIALS")
    if not cred_json:
        raise ValueError("FIREBASE_CREDENTIALS env var not set")
    cred = credentials.Certificate(json.loads(cred_json))
    firebase_admin.initialize_app(cred)
    return firestore.client()

# ============ PLAYINVEST (AÇÕES) ============
def fetch_playinvest(ticker):
    """Busca dividendos de ações no PlayInvest."""
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
            except (ValueError, AttributeError):
                continue
            if dc and rate > 0:
                divs.append({"dc": dc, "dp": dp or dc, "r": rate, "l": tipo})
        return divs
    except Exception as e:
        print(f"    PlayInvest error: {e}")
        return []

# ============ STATUSINVEST (FIIs) ============
def fetch_statusinvest_fii(ticker):
    """Busca dividendos de FIIs no StatusInvest via endpoint interno JSON."""
    url = f"https://statusinvest.com.br/fii/companytickerprovents?ticker={ticker}&chartProventsType=2"
    headers = {
        **HEADERS,
        "Referer": f"https://statusinvest.com.br/fundos-imobiliarios/{ticker.lower()}",
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"    StatusInvest HTTP {resp.status_code}")
            return []
        
        data = resp.json()
        divs = []
        
        # O endpoint retorna JSON com estrutura variável
        # Tentar diferentes formatos de resposta
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # Pode vir como {assetEarningsModels: [...]} ou {assetEarningsYearlyModels: [...]}
            items = data.get("assetEarningsModels", [])
            if not items:
                items = data.get("earningsThisYear", [])
            if not items:
                items = data.get("earningsLastYear", [])
            if not items:
                # Tentar pegar todos os valores de qualquer chave que seja lista
                for key, val in data.items():
                    if isinstance(val, list) and len(val) > 0:
                        items = val
                        break
        
        for item in items:
            if not isinstance(item, dict):
                continue
            # Extrair campos — StatusInvest usa vários nomes
            dc = item.get("ed") or item.get("lastDatePrior") or item.get("com") or ""
            dp = item.get("pd") or item.get("paymentDate") or item.get("payment") or ""
            rate = item.get("v") or item.get("value") or item.get("val") or 0
            label = item.get("et") or item.get("label") or item.get("type") or "RENDIMENTO"
            
            # Normalizar datas (pode vir como "2024-06-03T00:00:00" ou "03/06/2024")
            dc = normalize_date(dc)
            dp = normalize_date(dp)
            
            if isinstance(rate, str):
                try:
                    rate = float(rate.replace(",", "."))
                except:
                    continue
            
            if dc and rate and rate > 0:
                divs.append({"dc": dc, "dp": dp or dc, "r": rate, "l": str(label)})
        
        return divs
    except Exception as e:
        print(f"    StatusInvest error: {e}")
        return []

def normalize_date(d):
    """Normaliza datas em vários formatos para YYYY-MM-DD."""
    if not d:
        return None
    d = str(d).strip()
    # ISO format: 2024-06-03T00:00:00
    if "T" in d:
        return d[:10]
    # BR format: 03/06/2024
    if "/" in d:
        return parse_date_br(d)
    # Already YYYY-MM-DD
    if len(d) == 10 and d[4] == "-":
        return d
    return None

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
    
    # Load ativos
    ativos_doc = db.collection("investimentos").document("ativos").get()
    if not ativos_doc.exists:
        print("Nenhum ativo no Firestore.")
        return
    
    ativos = ativos_doc.to_dict().get("lista", [])
    carteira = [a for a in ativos if a.get("s") == "C"]
    
    # Separar ações/FIIs
    acoes = [a["k"] for a in carteira if a.get("tp") in ("AÇÕES",)]
    fiis = [a["k"] for a in carteira if a.get("tp") == "FII"]
    # ETFs e Crypto não têm dividendos relevantes
    
    print(f"Ações: {len(acoes)} → PlayInvest")
    print(f"FIIs:  {len(fiis)} → StatusInvest")
    
    # Load operations
    ops_doc = db.collection("investimentos").document("operacoes").get()
    operations = ops_doc.to_dict() if ops_doc.exists else {}
    
    # Load existing
    div_doc = db.collection("investimentos").document("dividendos").get()
    existing = div_doc.to_dict() if div_doc.exists else {}
    existing_dy = existing.get("dy_known", {})
    
    all_events = {}
    
    # --- AÇÕES via PlayInvest ---
    print(f"\n{'='*40}")
    print("AÇÕES — PlayInvest")
    print(f"{'='*40}")
    success_a = 0
    for i, tk in enumerate(acoes):
        print(f"[{i+1}/{len(acoes)}] {tk}...", end="")
        events = fetch_playinvest(tk)
        if events:
            all_events[tk] = events
            success_a += 1
            print(f" ✓ {len(events)} eventos")
        else:
            print(f" ✗ 0 eventos")
        time.sleep(DELAY)
    
    # --- FIIs via StatusInvest ---
    print(f"\n{'='*40}")
    print("FIIs — StatusInvest")
    print(f"{'='*40}")
    success_f = 0
    for i, tk in enumerate(fiis):
        print(f"[{i+1}/{len(fiis)}] {tk}...", end="")
        events = fetch_statusinvest_fii(tk)
        if events:
            all_events[tk] = events
            success_f += 1
            print(f" ✓ {len(events)} eventos")
        else:
            print(f" ✗ 0 eventos")
        time.sleep(DELAY)
    
    print(f"\nResumo fetch: Ações {success_a}/{len(acoes)}, FIIs {success_f}/{len(fiis)}")
    
    # Process
    print(f"\n{'='*40}")
    print("Calculando DY com sharesAt()...")
    print(f"{'='*40}")
    dy_known, monthly, filtered = process_dividends(all_events, operations)
    
    # Merge: keep existing for tickers that failed
    for tk, val in existing_dy.items():
        if tk not in dy_known:
            dy_known[tk] = val
    
    total_dy = sum(dy_known.values())
    print(f"DY total: R$ {total_dy:.2f} ({len(dy_known)} tickers)")
    print(f"Meses: {len(monthly)}")
    
    # Save
    db.collection("investimentos").document("dividendos").set({
        "dy_known": dy_known,
        "mensal": monthly,
        "eventos": filtered,
        "ultimaAtualizacao": datetime.now().isoformat(),
        "fonte": "PlayInvest (ações) + StatusInvest (FIIs)"
    })
    print("✓ Salvo no Firestore!")
    
    # Detail
    print(f"\n{'='*40}")
    print("Detalhe por ticker")
    print(f"{'='*40}")
    for tk in sorted(dy_known.keys()):
        n = len(filtered.get(tk, []))
        src = "PlayInvest" if tk in acoes else "StatusInvest" if tk in fiis else "existente"
        print(f"  {tk}: R$ {dy_known[tk]:.2f} ({n} eventos) [{src}]")

if __name__ == "__main__":
    main()
