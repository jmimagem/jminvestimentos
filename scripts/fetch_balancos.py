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

def build_ticker_map(companies):
    """Mapeia CNPJ → ticker usando cadastro CVM + nome da empresa."""
    
    # Mapa manual expandido: CNPJ → ticker (principais empresas B3)
    CNPJ_MAP = {
        '33.000.167': 'PETR4',  # Petrobras
        '00.000.000': 'BBAS3',  # Banco do Brasil
        '60.746.948': 'BBDC4',  # Bradesco
        '60.872.504': 'ITUB4',  # Itau Unibanco
        '61.532.644': 'ITSA4',  # Itausa
        '33.592.510': 'VALE3',  # Vale
        '84.429.695': 'WEGE3',  # WEG
        '02.916.265': 'B3SA3',  # B3
        '33.611.500': 'ELET3',  # Eletrobras
        '20.706.413': 'CMIG4',  # Cemig
        '76.535.764': 'CPLE6',  # Copel
        '47.960.950': 'ABEV3',  # Ambev
        '89.850.341': 'BRAP4',  # Bradespar
        '02.558.157': 'SUZB3',  # Suzano
        '42.150.391': 'JBSS3',  # JBS
        '02.919.555': 'PRIO3',  # PetroRio/PRIO
        '76.484.013': 'EZTC3',  # EZTEC
        '08.534.605': 'HYPE3',  # Hypera
        '61.585.865': 'CSNA3',  # CSN
        '04.034.187': 'PETR3',  # Petrobras ON
        '01.838.723': 'BBDC3',  # Bradesco ON
        '33.041.260': 'VBBR3',  # Vibra Energia
        '07.526.557': 'RENT3',  # Localiza
        '02.328.280': 'CCRO3',  # CCR
        '04.088.208': 'SBSP3',  # Sabesp
        '60.894.730': 'SANB11', # Santander
        '76.622.217': 'SBFG3',  # Grupo SBF
        '92.754.738': 'SLCE3',  # SLC Agrícola
        '87.456.562': 'ALOS3',  # Allos
        '02.474.103': 'TIMS3',  # TIM
        '02.429.144': 'VIVT3',  # Vivo/Telefonica
        '60.850.229': 'LREN3',  # Lojas Renner
        '47.508.411': 'HAPV3',  # Hapvida
        '02.800.026': 'RDOR3',  # Rede D'Or
        '01.532.247': 'PCAR3',  # Pão de Açúcar
        '13.574.594': 'MGLU3',  # Magazine Luiza
        '08.181.508': 'FLRY3',  # Fleury
        '04.310.392': 'MULT3',  # Multiplan
        '02.388.668': 'ENEV3',  # Eneva
        '03.220.438': 'EQTL3',  # Equatorial
        '15.527.906': 'ENGI11', # Energisa
        '06.981.180': 'CMIN3',  # CSN Mineração
        '14.011.962': 'AZUL4',  # Azul
        '07.689.002': 'RAIL3',  # Rumo
        '34.274.233': 'BRFS3',  # BRF
        '23.637.428': 'CRFB3',  # Carrefour
        '61.412.110': 'RADL3',  # Raia Drogasil
        '08.886.098': 'ASAI3',  # Assaí
        '73.178.600': 'CYRE3',  # Cyrela
        '07.628.528': 'COGN3',  # Cogna
        '04.065.791': 'CPFE3',  # CPFL
        '43.776.517': 'IGTI11', # Iguatemi
        '04.423.567': 'UGPA3',  # Ultrapar
        '23.865.067': 'YDUQ3',  # Yduqs
        '60.148.154': 'ECOR3',  # Ecorodovias
        '16.404.287': 'DXCO3',  # Dexco
        '02.762.115': 'MRVE3',  # MRV
        '97.837.181': 'GOAU4',  # Metalúrgica Gerdau
        '33.613.286': 'GGBR4',  # Gerdau
        '07.170.938': 'CASH3',  # Méliuz
        '08.063.988': 'LWSA3',  # LWSA
        '10.346.185': 'CVCB3',  # CVC
        '91.826.310': 'SMTO3',  # São Martinho
        '89.637.490': 'BRKM5',  # Braskem
        '04.184.779': 'CSAN3',  # Cosan
        '07.516.534': 'BEEF3',  # Minerva
        '09.346.601': 'RRRP3',  # 3R Petroleum
        '06.980.064': 'MRFG3',  # Marfrig
        '11.237.486': 'ALPA4',  # Alpargatas
        '02.302.101': 'IRBR3',  # IRB
        '76.487.032': 'KLBN11', # Klabin
        '89.073.104': 'KLBN4',  # Klabin
        '56.720.428': 'NTCO3',  # Natura
        '03.847.461': 'GOLL4',  # Gol
        '28.757.546': 'TOTS3',  # TOTVS
        '50.746.577': 'TAEE11', # Taesa
        '92.660.007': 'PETZ3',  # Petz/Cobasi
        '82.508.433': 'FESA4',  # Ferbasa
        '79.739.440': 'MYPK3',  # Iochpe-Maxion
        '44.990.901': 'ROMI3',  # Romi
        '88.610.126': 'GRND3',  # Grendene
        '81.423.838': 'POMO4',  # Marcopolo
        '12.644.536': 'SOJA3',  # Boa Safra
        '07.318.783': 'ABCB4',  # ABC Brasil
        '92.702.067': 'BRSR6',  # Banrisul
        '15.141.799': 'CAML3',  # Camil
        '30.306.294': 'BMGB4',  # BMG
        '33.014.556': 'ISAE4',  # ISA CTEEP
        '61.856.571': 'EGIE3',  # Engie
        '86.375.425': 'MTRE3',  # Mitre
        '80.659.624': 'TRIS3',  # Trisul
        '14.810.028': 'FIQE3',  # Unifique
        '60.651.809': 'CIEL3',  # Cielo
        '10.347.985': 'SOMA3',  # Grupo Soma
        '04.738.023': 'USIM5',  # Usiminas
        '47.427.653': 'BBSE3',  # BB Seguridade
    }
    
    # Mapear empresas CVM por CNPJ (primeiros 10 dígitos)
    ticker_map = {}
    mapped_tickers = set()
    
    for cnpj, data in companies.items():
        cnpj_prefix = cnpj[:10] if len(cnpj) >= 10 else cnpj
        
        # Tentar match por CNPJ
        for prefix, ticker in CNPJ_MAP.items():
            if cnpj.startswith(prefix) and ticker not in mapped_tickers:
                ticker_map[cnpj] = ticker
                mapped_tickers.add(ticker)
                break
        
        # Se não achou por CNPJ, tentar por nome
        if cnpj not in ticker_map:
            nome = data['nome'].upper().strip()
            nome_clean = nome.replace('.','').replace('-','').replace('/','').replace('S A','').replace('S/A','').strip()
            
            # Match por palavras-chave no nome
            NAME_MAP = {
                'PETROBRAS': 'PETR4', 'BANCO DO BRASIL': 'BBAS3', 'BRADESCO': 'BBDC4',
                'ITAU UNIBANCO': 'ITUB4', 'ITAUSA': 'ITSA4', 'VALE': 'VALE3',
                'WEG': 'WEGE3', 'AMBEV': 'ABEV3', 'SUZANO': 'SUZB3',
                'ELETROBRAS': 'ELET3', 'CEMIG': 'CMIG4', 'COPEL': 'CPLE6',
                'LOCALIZA': 'RENT3', 'SABESP': 'SBSP3', 'LOJAS RENNER': 'LREN3',
                'MAGAZINE LUIZA': 'MGLU3', 'FLEURY': 'FLRY3', 'MULTIPLAN': 'MULT3',
                'EQUATORIAL': 'EQTL3', 'CYRELA': 'CYRE3', 'ULTRAPAR': 'UGPA3',
                'GERDAU': 'GGBR4', 'KLABIN': 'KLBN11', 'NATURA': 'NTCO3',
                'TOTVS': 'TOTS3', 'MARCOPOLO': 'POMO4', 'GRENDENE': 'GRND3',
                'FERBASA': 'FESA4', 'ROMI': 'ROMI3', 'CAMIL': 'CAML3',
                'BANRISUL': 'BRSR6', 'TRISUL': 'TRIS3', 'MITRE': 'MTRE3',
                'HYPERA': 'HYPE3', 'ENGIE': 'EGIE3', 'VIVO': 'VIVT3',
                'RAIZEN': 'RAIZ4', 'COSAN': 'CSAN3', 'RUMO': 'RAIL3',
                'HAPVIDA': 'HAPV3', 'REDE DOR': 'RDOR3', 'BRF': 'BRFS3',
                'DEXCO': 'DXCO3', 'MRV': 'MRVE3', 'BRASKEM': 'BRKM5',
                'ALPARGATAS': 'ALPA4', 'IRB': 'IRBR3', 'CIELO': 'CIEL3',
                'USIMINAS': 'USIM5', 'BB SEGURIDADE': 'BBSE3', 'PRIO': 'PRIO3',
                'ENEVA': 'ENEV3', 'CSN': 'CSNA3', 'MARFRIG': 'MRFG3',
                'MINERVA': 'BEEF3', 'JBS': 'JBSS3', 'COGNA': 'COGN3',
                'CPFL': 'CPFE3', 'AZUL': 'AZUL4', 'GOL': 'GOLL4',
            }
            for keyword, ticker in NAME_MAP.items():
                if keyword in nome and ticker not in mapped_tickers:
                    ticker_map[cnpj] = ticker
                    mapped_tickers.add(ticker)
                    break
    
    return ticker_map

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
    
    # Mapear CNPJ → ticker usando cadastro CVM + mapa manual expandido
    ticker_map = build_ticker_map(companies)
    print(f"Tickers mapeados: {len(ticker_map)}")
    
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
