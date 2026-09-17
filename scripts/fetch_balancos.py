"""
Busca balanços da CVM (DFP/ITR) e calcula indicadores fundamentalistas.
Salva no Firestore para uso no dashboard e Analista Graham.
"""
import os
import json
import time
import zipfile
import io
import requests
import pandas as pd
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore

CVM_BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC"
HEADERS = {"User-Agent": "Mozilla/5.0"}

def init_firebase():
    cred = credentials.Certificate(json.loads(os.environ["FIREBASE_CREDENTIALS"]))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()

def download_cvm_csv(doc_type, year):
    """Baixa e extrai CSV da CVM. doc_type: DFP ou ITR."""
    url = f"{CVM_BASE}/{doc_type}/DADOS/{doc_type.lower()}_cia_aberta_{year}.zip"
    print(f"  Baixando {url}...")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=60)
        if resp.status_code != 200:
            print(f"  ✗ HTTP {resp.status_code}")
            return {}
        
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        dfs = {}
        for name in zf.namelist():
            if name.endswith('.csv'):
                # Extrair tipo: dfp_cia_aberta_DRE_con_2024.csv → DRE_CON
                parts = name.split('_cia_aberta_')
                if len(parts) > 1:
                    key = parts[1].replace('.csv', '').upper()
                    # Remover o ano do final: DRE_CON_2024 → DRE_CON
                    for y in ['_2024', '_2025', '_2026', '_2027']:
                        key = key.replace(y, '')
                else:
                    key = name.replace('.csv', '').upper()
                try:
                    df = pd.read_csv(zf.open(name), sep=';', encoding='latin-1', 
                                     dtype=str, on_bad_lines='skip')
                    dfs[key] = df
                    print(f"    {key}: {len(df)} linhas")
                except Exception as e:
                    print(f"    {key}: erro {e}")
        return dfs
    except Exception as e:
        print(f"  ✗ Erro: {e}")
        return {}

def extract_financials(dfs_by_year):
    """Extrai dados financeiros consolidados por empresa/ticker."""
    companies = {}  # {CNPJ: {nome, dados por periodo}}
    
    for year, dfs in dfs_by_year.items():
        # DRE - Demonstração de Resultado (consolidado primeiro, individual como fallback)
        dre_key = [k for k in dfs.keys() if 'DRE' in k and 'CON' in k]
        if not dre_key:
            dre_key = [k for k in dfs.keys() if 'DRE' in k]
        if dre_key:
            df = dfs[dre_key[0]]
            # Filtrar consolidado (ORDEM_EXERC=ÚLTIMO, GRUPO_DFP=DF Consolidado)
            if 'ORDEM_EXERC' in df.columns:
                df = df[df['ORDEM_EXERC'] == 'ÚLTIMO']
            if 'GRUPO_DFP' in df.columns:
                df_cons = df[df['GRUPO_DFP'].str.contains('Consolidado', na=False)]
                if len(df_cons) > 0:
                    df = df_cons
            
            for _, row in df.iterrows():
                cnpj = str(row.get('CNPJ_CIA', '')).strip()
                nome = str(row.get('DENOM_CIA', '')).strip()
                dt_ref = str(row.get('DT_REFER', '')).strip()
                conta = str(row.get('CD_CONTA', '')).strip()
                desc = str(row.get('DS_CONTA', '')).strip()
                try:
                    valor = float(str(row.get('VL_CONTA', '0')).replace(',', '.')) * 1000  # CVM em milhares
                except:
                    valor = 0
                
                if not cnpj or not dt_ref:
                    continue
                
                if cnpj not in companies:
                    companies[cnpj] = {'nome': nome, 'periodos': {}}
                
                if dt_ref not in companies[cnpj]['periodos']:
                    companies[cnpj]['periodos'][dt_ref] = {}
                
                # Mapear contas relevantes
                if conta == '3.01':
                    companies[cnpj]['periodos'][dt_ref]['receita'] = valor
                elif conta == '3.11':
                    companies[cnpj]['periodos'][dt_ref]['lucro_liquido'] = valor
                elif conta == '3.05':
                    companies[cnpj]['periodos'][dt_ref]['ebit'] = valor
        
        # BPP - Balanço Patrimonial Passivo (Patrimônio Líquido)
        bpp_key = [k for k in dfs.keys() if 'BPP' in k and 'CON' in k]
        if not bpp_key:
            bpp_key = [k for k in dfs.keys() if 'BPP' in k]
        if bpp_key:
            df = dfs[bpp_key[0]]
            if 'ORDEM_EXERC' in df.columns:
                df = df[df['ORDEM_EXERC'] == 'ÚLTIMO']
            if 'GRUPO_DFP' in df.columns:
                df_cons = df[df['GRUPO_DFP'].str.contains('Consolidado', na=False)]
                if len(df_cons) > 0:
                    df = df_cons
            
            for _, row in df.iterrows():
                cnpj = str(row.get('CNPJ_CIA', '')).strip()
                dt_ref = str(row.get('DT_REFER', '')).strip()
                conta = str(row.get('CD_CONTA', '')).strip()
                try:
                    valor = float(str(row.get('VL_CONTA', '0')).replace(',', '.')) * 1000
                except:
                    valor = 0
                
                if not cnpj or not dt_ref:
                    continue
                if cnpj not in companies:
                    continue
                if dt_ref not in companies[cnpj]['periodos']:
                    companies[cnpj]['periodos'][dt_ref] = {}
                
                if conta == '2.03':  # Patrimônio Líquido Consolidado
                    companies[cnpj]['periodos'][dt_ref]['patrimonio_liquido'] = valor
                elif conta == '2.01':  # Passivo Circulante
                    companies[cnpj]['periodos'][dt_ref]['passivo_circulante'] = valor
                elif conta == '2.02':  # Passivo Não Circulante
                    companies[cnpj]['periodos'][dt_ref]['passivo_nao_circulante'] = valor
        
        # BPA - Balanço Patrimonial Ativo
        bpa_key = [k for k in dfs.keys() if 'BPA' in k and 'CON' in k]
        if not bpa_key:
            bpa_key = [k for k in dfs.keys() if 'BPA' in k]
        if bpa_key:
            df = dfs[bpa_key[0]]
            if 'ORDEM_EXERC' in df.columns:
                df = df[df['ORDEM_EXERC'] == 'ÚLTIMO']
            if 'GRUPO_DFP' in df.columns:
                df_cons = df[df['GRUPO_DFP'].str.contains('Consolidado', na=False)]
                if len(df_cons) > 0:
                    df = df_cons
            
            for _, row in df.iterrows():
                cnpj = str(row.get('CNPJ_CIA', '')).strip()
                dt_ref = str(row.get('DT_REFER', '')).strip()
                conta = str(row.get('CD_CONTA', '')).strip()
                try:
                    valor = float(str(row.get('VL_CONTA', '0')).replace(',', '.')) * 1000
                except:
                    valor = 0
                
                if not cnpj or cnpj not in companies:
                    continue
                if dt_ref not in companies[cnpj].get('periodos', {}):
                    continue
                
                if conta == '1':  # Ativo Total
                    companies[cnpj]['periodos'][dt_ref]['ativo_total'] = valor
                elif conta == '1.01':  # Ativo Circulante
                    companies[cnpj]['periodos'][dt_ref]['ativo_circulante'] = valor
    
    return companies

def calculate_metrics(companies, ticker_map):
    """Calcula indicadores fundamentalistas por ticker."""
    results = {}
    
    for cnpj, data in companies.items():
        ticker = ticker_map.get(cnpj)
        if not ticker:
            continue
        
        periodos = sorted(data['periodos'].keys(), reverse=True)
        if not periodos:
            continue
        
        metrics = {
            'nome': data['nome'],
            'periodos_disponiveis': periodos[:8],
            'historico': []
        }
        
        for dt in periodos[:8]:  # Últimos 8 períodos
            p = data['periodos'][dt]
            rec = p.get('receita', 0)
            ll = p.get('lucro_liquido', 0)
            pl_val = p.get('patrimonio_liquido', 0)
            ebit = p.get('ebit', 0)
            at = p.get('ativo_total', 0)
            pc = p.get('passivo_circulante', 0)
            pnc = p.get('passivo_nao_circulante', 0)
            
            entry = {
                'periodo': dt,
                'receita': round(rec / 1e6, 2),  # Em milhões
                'lucro_liquido': round(ll / 1e6, 2),
                'patrimonio_liquido': round(pl_val / 1e6, 2),
                'ebit': round(ebit / 1e6, 2),
                'ativo_total': round(at / 1e6, 2),
                'divida_bruta': round((pc + pnc) / 1e6, 2),
                'margem_liquida': round(ll / rec * 100, 2) if rec else 0,
                'roe': round(ll / pl_val * 100, 2) if pl_val else 0,
                'divida_pl': round((pc + pnc) / pl_val, 2) if pl_val else 0,
                'margem_ebit': round(ebit / rec * 100, 2) if rec else 0,
            }
            metrics['historico'].append(entry)
        
        # Calcular crescimento
        if len(metrics['historico']) >= 2:
            h = metrics['historico']
            rec_atual = h[0]['receita']
            rec_ant = h[1]['receita']
            ll_atual = h[0]['lucro_liquido']
            ll_ant = h[1]['lucro_liquido']
            
            metrics['cresc_receita'] = round((rec_atual - rec_ant) / abs(rec_ant) * 100, 2) if rec_ant else 0
            metrics['cresc_lucro'] = round((ll_atual - ll_ant) / abs(ll_ant) * 100, 2) if ll_ant else 0
        
        # Último período como resumo
        if metrics['historico']:
            last = metrics['historico'][0]
            metrics['ultimo_periodo'] = last['periodo']
            metrics['margem_liquida'] = last['margem_liquida']
            metrics['roe'] = last['roe']
            metrics['divida_pl'] = last['divida_pl']
            metrics['receita_mm'] = last['receita']
            metrics['lucro_mm'] = last['lucro_liquido']
        
        results[ticker] = metrics
    
    return results

def load_ticker_cnpj_map():
    """Carrega mapeamento ticker → CNPJ do cadastro CVM."""
    url = "https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv"
    try:
        df = pd.read_csv(url, sep=';', encoding='latin-1', dtype=str)
        ticker_map = {}  # CNPJ → ticker
        for _, row in df.iterrows():
            cnpj = str(row.get('CNPJ_CIA', '')).strip()
            cd_cvm = str(row.get('CD_CVM', '')).strip()
            nome = str(row.get('DENOM_SOCIAL', '')).strip()
            sit = str(row.get('SIT_REG', '')).strip()
            if sit == 'ATIVO' and cnpj:
                ticker_map[cnpj] = {'cd_cvm': cd_cvm, 'nome': nome}
        return ticker_map
    except Exception as e:
        print(f"Erro carregando cadastro CVM: {e}")
        return {}

def build_cnpj_ticker_map(fundamentos):
    """Constrói mapa CNPJ → ticker usando dados do yfinance (que tem o CNPJ em some cases)."""
    # Mapeamento manual dos principais tickers brasileiros
    # Fonte: B3/CVM cadastro
    MANUAL_MAP = {
        '33.000.167/0001-01': 'PETR4', '33.000.167/0002-93': 'PETR3',
        '00.000.000/0001-91': 'BBAS3',
        '60.746.948/0001-12': 'BBDC4', '60.746.948/0002-03': 'BBDC3',
        '60.872.504/0001-23': 'ITUB4',
        '61.532.644/0001-15': 'ITSA4',
        '33.592.510/0001-54': 'VALE3',
        '84.429.695/0001-11': 'WEGE3',
        '02.916.265/0001-60': 'B3SA3',
        '33.611.500/0001-19': 'ELET3',
        '20.706.413/0001-07': 'CMIG4',
        '76.535.764/0001-43': 'CPLE6', '76.535.764/0002-24': 'CPLE3',
        '47.960.950/0001-21': 'ABEV3',
        '89.850.341/0001-60': 'BRAP4',
        '02.558.157/0001-62': 'SUZB3',
        '42.150.391/0001-70': 'JBSS3',
        '02.919.555/0001-67': 'PRIO3',
        '76.484.013/0001-45': 'EZTC3',
        '08.534.605/0001-74': 'HYPE3',
        '61.585.865/0001-51': 'CSNA3',
    }
    return {v: k for k, v in MANUAL_MAP.items()}  # ticker → CNPJ invertido pra busca

def main():
    print("=" * 60)
    print(f"CVM Balanços — {datetime.now().isoformat()}")
    print("=" * 60)
    
    db = init_firebase()
    
    # Baixar dados dos últimos 3 anos
    current_year = datetime.now().year
    years = [current_year, current_year - 1, current_year - 2]
    
    all_dfs = {}
    for year in years:
        print(f"\n--- DFP {year} ---")
        dfs = download_cvm_csv("DFP", year)
        if dfs:
            all_dfs[f"DFP_{year}"] = dfs
        
        print(f"\n--- ITR {year} ---")
        dfs = download_cvm_csv("ITR", year)
        if dfs:
            all_dfs[f"ITR_{year}"] = dfs
    
    if not all_dfs:
        print("Nenhum dado obtido da CVM")
        return
    
    # Extrair dados financeiros
    print(f"\n{'='*40}")
    print("Processando balanços...")
    companies = extract_financials(all_dfs)
    print(f"Empresas encontradas: {len(companies)}")
    
    # Mapear CNPJ → ticker (simplificado)
    # Na prática, usamos os tickers do fundamentos no Firestore
    fund_doc = db.collection("investimentos").document("fundamentos").get()
    fund_data = fund_doc.to_dict() if fund_doc.exists else {}
    
    # Criar mapa reverso: tentar mapear pelo nome da empresa
    ticker_map = {}
    for cnpj, data in companies.items():
        nome_cvm = data['nome'].upper()
        for tk, fund in fund_data.items():
            if isinstance(fund, dict) and fund.get('name'):
                nome_fund = fund['name'].upper()
                # Match parcial pelo nome
                if (nome_cvm[:15] in nome_fund or nome_fund[:15] in nome_cvm or
                    tk in nome_cvm.replace(' ', '')):
                    ticker_map[cnpj] = tk
                    break
    
    print(f"Tickers mapeados: {len(ticker_map)}")
    
    # Calcular métricas
    results = calculate_metrics(companies, ticker_map)
    print(f"Análises geradas: {len(results)}")
    
    # Salvar no Firestore
    if results:
        results['ultimaAtualizacao'] = datetime.now().isoformat()
        db.collection("investimentos").document("balancos").set(results)
        print(f"✓ Salvo no Firestore: {len(results)-1} empresas")
        
        # Resumo
        for tk in sorted(results.keys()):
            if tk == 'ultimaAtualizacao':
                continue
            r = results[tk]
            print(f"  {tk}: ROE={r.get('roe',0)}% ML={r.get('margem_liquida',0)}% D/PL={r.get('divida_pl',0)} Rec={r.get('receita_mm',0)}MM")

if __name__ == "__main__":
    main()
