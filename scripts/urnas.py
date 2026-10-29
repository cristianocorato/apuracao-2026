#!/usr/bin/env python3
"""
Dados por urna do 2º turno para presidente (Eleições 2026), para o mapa do site.

Fontes (Portal de Dados Abertos do TSE, https://dadosabertos.tse.jus.br):
  - Boletins de urna do 2º turno, um arquivo por estado e um do exterior
    (conjunto "resultados-2026-boletim-de-urna", arquivos bweb_2t_UF_*.zip);
  - Eleitorado por local de votação 2026, com nome, endereço e coordenadas
    de cada local (conjunto "eleitorado-2026").

Gera:
  dados/urnas/<uf>.json   locais de votação do estado, cada um com as suas urnas
  dados/urnas/resumo.json totais, horários de encerramento e urnas encerradas
                          depois das 17h

Horários: a votação foi das 8h às 17h de Brasília em todo o país (no exterior,
17h no horário local). A urna registra o horário local, por isso o script
descobre o horário oficial de encerramento de cada cidade pelos próprios
boletins (a maioria das urnas fecha na hora) e converte tudo para o horário de
Brasília. No exterior, os horários ficam no horário local.

Só usa a biblioteca padrão do Python 3. Uso:  python3 scripts/urnas.py
Para testar sem internet: URNAS_PASTA_LOCAL=/pasta/com/os/zips python3 scripts/urnas.py
"""

import csv
import hashlib
import io
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone

CKAN = 'https://dadosabertos.tse.jus.br/api/3/action/package_show?id='
PACOTE_BU = 'resultados-2026-boletim-de-urna'
BASE_BU = 'https://cdn.tse.jus.br/estatistica/sead/eleicoes/eleicoes2026/buweb'
CARIMBO_BU = '261020260742'        # data e hora dos arquivos do 2º turno publicados pelo TSE
PACOTE_LOCAIS = 'eleitorado-2026'
URL_LOCAIS = ('https://cdn.tse.jus.br/estatistica/sead/odsele/eleitorado_locais_votacao/'
              'eleitorado_local_votacao_2026.zip')
DIA = (2026, 10, 25)              # 2º turno
CANDIDATOS = ('13', '22')          # Lula, Flávio Bolsonaro
SAIDA = os.environ.get('URNAS_SAIDA', 'dados/urnas')
TEMP = os.environ.get('RUNNER_TEMP') or os.environ.get('TMPDIR') or '/tmp'
PASTA_LOCAL = os.environ.get('URNAS_PASTA_LOCAL', '')
UFS = ['ac', 'al', 'am', 'ap', 'ba', 'ce', 'df', 'es', 'go', 'ma', 'mg', 'ms', 'mt', 'pa', 'pb',
       'pe', 'pi', 'pr', 'rj', 'rn', 'ro', 'rr', 'rs', 'sc', 'se', 'sp', 'to', 'zz']

csv.field_size_limit(10_000_000)


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def aviso(msg):
    print('::warning::' + msg, flush=True)


# ---------------------------------------------------------------------------
# Download

def baixar(url, destino=None, tentativas=4):
    for t in range(1, tentativas + 1):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; apuracao-2026/1.0; site independente)'})
            with urllib.request.urlopen(req, timeout=120) as r:
                if destino is None:
                    return r.read()
                with open(destino, 'wb') as f:
                    while True:
                        bloco = r.read(1 << 20)
                        if not bloco:
                            break
                        f.write(bloco)
                return destino
        except Exception as e:  # noqa: BLE001
            if t == tentativas:
                raise
            log(f'  tentativa {t} falhou ({e}); tentando de novo')
            time.sleep(5 * t)


def recursos(pacote):
    j = json.loads(baixar(CKAN + pacote).decode('utf-8'))
    return j['result']['resources']


def existe(url):
    try:
        req = urllib.request.Request(url, method='HEAD', headers={
            'User-Agent': 'Mozilla/5.0 (compatible; apuracao-2026/1.0; site independente)'})
        with urllib.request.urlopen(req, timeout=60) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        return False


def arquivos_bu():
    """UF -> (url do zip, url do hash) dos boletins de urna do 2º turno."""
    achados = {}
    try:
        res = recursos(PACOTE_BU)
        log(f'  {len(res)} recursos no conjunto {PACOTE_BU}; exemplos: '
            + ' | '.join(str(r.get('url') or r.get('name'))[-70:] for r in res[:4]))
        for r in res:
            url = (r.get('url') or '').strip()
            m = re.search(r'bweb_2t_([A-Za-z]{2})_\d+\.zip', url, re.I)
            if not m:
                continue
            uf = m.group(1).lower()
            zip_url = url[:url.lower().index('.zip') + 4]  # alguns recursos apontam para o .sha512 por engano
            achados[uf] = (zip_url, zip_url + '.sha512')
    except Exception as e:  # noqa: BLE001
        aviso(f'Não consegui ler a lista do Portal de Dados Abertos: {e}')
    # O que faltar: tenta o endereço de sempre do TSE, com o mesmo carimbo de data dos outros arquivos
    carimbos = {m.group(1) for u, _ in achados.values() for m in [re.search(r'_(\d{12})\.zip', u)] if m}
    carimbos.add(CARIMBO_BU)
    for uf in UFS:
        if uf in achados:
            continue
        for c in sorted(carimbos):
            url = f'{BASE_BU}/bweb_2t_{uf.upper()}_{c}.zip'
            if existe(url):
                achados[uf] = (url, url + '.sha512')
                break
    return achados


def conferir_hash(caminho, url_hash):
    try:
        esperado = baixar(url_hash).decode('ascii', 'ignore').split()[0].strip().lower()
    except Exception as e:  # noqa: BLE001
        aviso(f'Não consegui baixar o hash {url_hash}: {e}')
        return None
    h = hashlib.sha512()
    with open(caminho, 'rb') as f:
        for bloco in iter(lambda: f.read(1 << 20), b''):
            h.update(bloco)
    ok = h.hexdigest().lower() == esperado
    if not ok:
        aviso(f'Hash diferente do publicado pelo TSE em {os.path.basename(caminho)}')
    return ok


def obter_zip(nome_arquivo, url, url_hash=None):
    if PASTA_LOCAL:
        caminho = os.path.join(PASTA_LOCAL, nome_arquivo)
        return caminho if os.path.exists(caminho) else None
    caminho = os.path.join(TEMP, nome_arquivo)
    t0 = time.time()
    baixar(url, caminho)
    tam = os.path.getsize(caminho) / 1e6
    ok = conferir_hash(caminho, url_hash) if url_hash else None
    log(f'  {nome_arquivo}: {tam:.1f} MB em {time.time() - t0:.0f} s' + ('' if ok is None else (' (hash ok)' if ok else ' (HASH DIFERENTE)')))
    return caminho


def linhas_csv(caminho_zip):
    """Lê o primeiro CSV de um zip do TSE (latin-1, ';') e devolve (cabeçalho, leitor)."""
    z = zipfile.ZipFile(caminho_zip)
    nomes = [n for n in z.namelist() if n.lower().endswith('.csv')]
    if not nomes:
        raise RuntimeError(f'Nenhum CSV em {caminho_zip}: {z.namelist()[:5]}')
    nomes.sort(key=lambda n: -z.getinfo(n).file_size)
    f = io.TextIOWrapper(z.open(nomes[0]), encoding='latin-1', newline='')
    leitor = csv.reader(f, delimiter=';', quotechar='"')
    cab = [c.strip().strip('"').upper() for c in next(leitor)]
    return nomes[0], cab, leitor


def coluna(cab, *nomes, obrigatoria=True):
    for n in nomes:
        if n in cab:
            return cab.index(n)
    if obrigatoria:
        raise RuntimeError(f'Coluna não encontrada: {nomes} (cabeçalho: {cab})')
    return None


def inteiro(v):
    try:
        return int(str(v).strip())
    except ValueError:
        return 0


def texto(v):
    v = (v or '').strip()
    return '' if v.upper() in ('#NULO#', '#NULO', '#NE#', '#NE', 'NULO', '-1', '-3', 'N/A') else v


def coordenada(v, minimo, maximo):
    v = (v or '').strip().replace(',', '.')
    try:
        x = float(v)
    except ValueError:
        return None
    if x == 0 or x == -1 or not (minimo <= x <= maximo) or math.isnan(x):
        return None
    return round(x, 5)


def segundos_do_dia(v):
    """'25/10/2026 17:03:12' ou '2026-10-25 17:03:12' -> segundos desde 0h do dia da eleição."""
    v = (v or '').strip()
    if not v or v.startswith('#'):
        return None
    for formato in ('%d/%m/%Y %H:%M:%S', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y %H:%M', '%d/%m/%y %H:%M:%S'):
        try:
            d = datetime.strptime(v, formato)
            break
        except ValueError:
            continue
    else:
        return None
    dias = (d.date() - datetime(*DIA).date()).days
    return dias * 86400 + d.hour * 3600 + d.minute * 60 + d.second


def hhmm(seg):
    if seg is None:
        return ''
    s = int(seg) % 86400
    return f'{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}'


# ---------------------------------------------------------------------------
# Locais de votação

def carregar_locais():
    caminho = obter_zip('eleitorado_local_votacao_2026.zip', URL_LOCAIS) if not PASTA_LOCAL else \
        obter_zip('eleitorado_local_votacao_2026.zip', None)
    if not caminho:
        aviso('Arquivo de locais de votação não encontrado: o mapa vai sair sem endereços.')
        return {}, {}, {}
    nome, cab, leitor = linhas_csv(caminho)
    log(f'Locais de votação: {nome}')
    log('  colunas: ' + ', '.join(cab))
    i_uf = coluna(cab, 'SG_UF')
    i_mun = coluna(cab, 'CD_MUNICIPIO')
    i_nmun = coluna(cab, 'NM_MUNICIPIO', obrigatoria=False)
    i_zona = coluna(cab, 'NR_ZONA')
    i_secao = coluna(cab, 'NR_SECAO')
    i_local = coluna(cab, 'NR_LOCAL_VOTACAO')
    i_nome = coluna(cab, 'NM_LOCAL_VOTACAO')
    i_end = coluna(cab, 'DS_ENDERECO', 'DS_ENDERECO_LOCAL_VOTACAO', obrigatoria=False)
    i_bairro = coluna(cab, 'NM_BAIRRO', obrigatoria=False)
    i_cep = coluna(cab, 'NR_CEP', obrigatoria=False)
    i_lat = coluna(cab, 'NR_LATITUDE', obrigatoria=False)
    i_lon = coluna(cab, 'NR_LONGITUDE', obrigatoria=False)
    i_tipo = coluna(cab, 'DS_TIPO_LOCAL', obrigatoria=False)
    i_turno = coluna(cab, 'NR_TURNO', obrigatoria=False)

    locais = {}   # (uf, mun, zona, local) -> dados do local
    secoes = {}   # (uf, mun, zona, secao) -> chave do local
    nomes_mun = {}
    turnos = Counter()
    exemplos = 0
    for lin in leitor:
        if len(lin) < len(cab):
            continue
        turno = lin[i_turno].strip() if i_turno is not None else ''
        turnos[turno] += 1
        uf = lin[i_uf].strip().lower()
        mun = lin[i_mun].strip().zfill(5)
        zona = inteiro(lin[i_zona])
        chave_local = (uf, mun, zona, inteiro(lin[i_local]))
        chave_secao = (uf, mun, zona, inteiro(lin[i_secao]))
        if i_nmun is not None:
            nomes_mun[(uf, mun)] = texto(lin[i_nmun])
        if exemplos < 2:
            log('  exemplo: ' + ' | '.join(f'{c}={lin[i]}' for i, c in enumerate(cab)
                                          if c in ('SG_UF', 'NM_MUNICIPIO', 'NR_ZONA', 'NR_SECAO', 'NR_LOCAL_VOTACAO',
                                                   'NM_LOCAL_VOTACAO', 'DS_ENDERECO', 'NM_BAIRRO', 'NR_LATITUDE',
                                                   'NR_LONGITUDE', 'DS_TIPO_LOCAL', 'NR_TURNO')))
            exemplos += 1
        # Se houver as duas versões (1º e 2º turno), a do 2º turno vale
        if chave_secao in secoes and turno != '2':
            continue
        if uf == 'zz':
            lat, lon = coordenada(lin[i_lat] if i_lat is not None else '', -90, 90), \
                coordenada(lin[i_lon] if i_lon is not None else '', -180, 180)
        else:
            lat, lon = coordenada(lin[i_lat] if i_lat is not None else '', -34.5, 6), \
                coordenada(lin[i_lon] if i_lon is not None else '', -75, -28)
        if chave_local not in locais or turno == '2':
            locais[chave_local] = {
                'n': texto(lin[i_nome]),
                'e': texto(lin[i_end]) if i_end is not None else '',
                'b': texto(lin[i_bairro]) if i_bairro is not None else '',
                'c': texto(lin[i_cep]) if i_cep is not None else '',
                't': texto(lin[i_tipo]) if i_tipo is not None else '',
                'la': lat if lat is not None and lon is not None else None,
                'lo': lon if lat is not None and lon is not None else None,
            }
        secoes[chave_secao] = chave_local
    com_coord = sum(1 for v in locais.values() if v['la'] is not None)
    log(f'  {len(secoes)} seções em {len(locais)} locais ({com_coord} com coordenadas); turnos no arquivo: {dict(turnos)}')
    return locais, secoes, nomes_mun


# ---------------------------------------------------------------------------
# Boletins de urna

def ler_boletins(caminho, uf):
    """Devolve {(mun, zona, secao): dados da urna} só com o cargo de presidente."""
    nome, cab, leitor = linhas_csv(caminho)
    i = {
        'mun': coluna(cab, 'CD_MUNICIPIO'),
        'nmun': coluna(cab, 'NM_MUNICIPIO'),
        'zona': coluna(cab, 'NR_ZONA'),
        'secao': coluna(cab, 'NR_SECAO'),
        'local': coluna(cab, 'NR_LOCAL_VOTACAO', obrigatoria=False),
        'cargo': coluna(cab, 'CD_CARGO_PERGUNTA', 'CD_CARGO', obrigatoria=False),
        'dscargo': coluna(cab, 'DS_CARGO_PERGUNTA', 'DS_CARGO', obrigatoria=False),
        'aptos': coluna(cab, 'QT_APTOS'),
        'comp': coluna(cab, 'QT_COMPARECIMENTO'),
        'tipo_votavel': coluna(cab, 'DS_TIPO_VOTAVEL', obrigatoria=False),
        'cd_tipo_votavel': coluna(cab, 'CD_TIPO_VOTAVEL', obrigatoria=False),
        'votavel': coluna(cab, 'NR_VOTAVEL'),
        'votos': coluna(cab, 'QT_VOTOS'),
        'tipo_urna': coluna(cab, 'DS_TIPO_URNA', obrigatoria=False),
        'agregadas': coluna(cab, 'DS_AGREGADAS', obrigatoria=False),
        'abertura': coluna(cab, 'DT_ABERTURA', obrigatoria=False),
        'encerramento': coluna(cab, 'DT_ENCERRAMENTO', obrigatoria=False),
        'emissao': coluna(cab, 'DT_EMISSAO_BU', obrigatoria=False),
    }
    if not getattr(ler_boletins, 'cab_mostrado', False):
        log(f'  {nome} colunas: ' + ', '.join(cab))
        ler_boletins.cab_mostrado = True
    exemplos = 0
    urnas = {}
    nomes_mun = {}
    cargos = Counter()
    for lin in leitor:
        if len(lin) < len(cab):
            continue
        cd = lin[i['cargo']].strip() if i['cargo'] is not None else ''
        ds = lin[i['dscargo']].strip() if i['dscargo'] is not None else ''
        cargos[ds or cd] += 1
        if not (cd == '1' or ds.lower().startswith('presidente')):
            continue
        if exemplos < 2 and uf in ('ac', 'sp', 'zz'):
            log(f'  exemplo {uf.upper()}: ' + ' | '.join(f'{c}={lin[j]}' for j, c in enumerate(cab) if c in (
                'NM_MUNICIPIO', 'NR_ZONA', 'NR_SECAO', 'NR_LOCAL_VOTACAO', 'QT_APTOS', 'QT_COMPARECIMENTO',
                'DS_TIPO_URNA', 'DS_TIPO_VOTAVEL', 'NR_VOTAVEL', 'QT_VOTOS', 'DS_AGREGADAS', 'DT_ABERTURA',
                'DT_ENCERRAMENTO', 'DT_EMISSAO_BU', 'DT_BU_RECEBIDO')))
            exemplos += 1
        mun = lin[i['mun']].strip().zfill(5)
        chave = (mun, inteiro(lin[i['zona']]), inteiro(lin[i['secao']]))
        u = urnas.get(chave)
        if u is None:
            nomes_mun[mun] = lin[i['nmun']].strip()
            u = urnas[chave] = {
                'local': inteiro(lin[i['local']]) if i['local'] is not None else 0,
                'aptos': inteiro(lin[i['aptos']]),
                'comp': inteiro(lin[i['comp']]),
                'v': [0, 0], 'b': 0, 'n': 0,
                'tipo': lin[i['tipo_urna']].strip() if i['tipo_urna'] is not None else '',
                'agregadas': texto(lin[i['agregadas']]) if i['agregadas'] is not None else '',
                'ab': segundos_do_dia(lin[i['abertura']]) if i['abertura'] is not None else None,
                'enc': segundos_do_dia(lin[i['encerramento']]) if i['encerramento'] is not None else None,
                'emi': segundos_do_dia(lin[i['emissao']]) if i['emissao'] is not None else None,
                'enc_txt': lin[i['encerramento']].strip() if i['encerramento'] is not None else '',
            }
        nr = lin[i['votavel']].strip()
        qt = inteiro(lin[i['votos']])
        tv = (lin[i['tipo_votavel']].strip().lower() if i['tipo_votavel'] is not None else '')
        if nr in CANDIDATOS:
            u['v'][CANDIDATOS.index(nr)] += qt
        elif tv.startswith('branco') or nr == '95':
            u['b'] += qt
        elif tv.startswith('nulo') or nr == '96':
            u['n'] += qt
        else:
            u['n'] += qt  # anulados / outros entram como nulos
    return urnas, nomes_mun, cargos


def hora_oficial_local(encerramentos):
    """Hora oficial de encerramento no relógio local, a partir dos horários das urnas."""
    validos = sorted(s for s in encerramentos if s is not None and 10 * 3600 < s < 23 * 3600)
    if not validos:
        return None
    p25 = validos[max(0, int(len(validos) * 0.25) - 1)]
    return int(round(p25 / 3600))


# ---------------------------------------------------------------------------

def main():
    t0 = time.time()
    os.makedirs(SAIDA, exist_ok=True)

    if PASTA_LOCAL:
        fontes = {uf: (f'bweb_2t_{uf.upper()}.zip', None) for uf in UFS}
    else:
        log('Procurando os boletins de urna do 2º turno no Portal de Dados Abertos do TSE...')
        fontes = arquivos_bu()
        log(f'  {len(fontes)} arquivos: ' + ', '.join(sorted(fontes)))
        faltando = [uf for uf in UFS if uf not in fontes]
        if faltando:
            aviso('Sem boletins de urna para: ' + ', '.join(faltando))

    locais, secoes, nomes_mun_locais = carregar_locais()

    resumo_ufs = {}
    todas = []           # (uf, mun, zona, secao, enc_brasilia, urna) para os totais
    fusos = {}
    sem_local = 0
    sem_coord = 0
    tipos_urna = Counter()
    cargos_total = Counter()

    for uf in UFS:
        if uf not in fontes:
            continue
        url, url_hash = fontes[uf]
        nome_zip = os.path.basename(url) if not PASTA_LOCAL else url
        caminho = obter_zip(nome_zip, url, url_hash)
        if not caminho:
            aviso(f'{uf.upper()}: arquivo não encontrado')
            continue
        urnas, nomes_mun, cargos = ler_boletins(caminho, uf)
        cargos_total.update(cargos)
        if not PASTA_LOCAL:
            os.remove(caminho)

        # Hora oficial de encerramento no relógio de cada cidade (fuso), pelos próprios boletins
        por_mun = defaultdict(list)
        for (mun, _, _), u in urnas.items():
            por_mun[mun].append(u['enc'])
        hora_uf = hora_oficial_local([s for lst in por_mun.values() for s in lst])
        oficial = {}
        for mun, lst in por_mun.items():
            h = hora_oficial_local(lst) if sum(1 for s in lst if s is not None) >= 3 else None
            oficial[mun] = h if h is not None else hora_uf
        fusos[uf] = dict(Counter(oficial.values()))

        if uf == 'zz':
            diferentes = {m: h for m, h in oficial.items() if h is not None and h != 17}
            if diferentes:
                aviso(f'Exterior: {len(diferentes)} cidades com hora oficial local diferente de 17h: {diferentes}')

        # Monta os locais do estado
        tipos_uf = []
        locais_uf = {}
        for (mun, zona, secao), u in sorted(urnas.items()):
            tipos_urna[u['tipo']] += 1
            if u['tipo'] not in tipos_uf:
                tipos_uf.append(u['tipo'])
            h = oficial.get(mun)
            # No Brasil, tudo em horário de Brasília (17h); no exterior, no horário local
            desloc = 0 if (uf == 'zz' or h is None) else (17 - h) * 3600
            enc = None if u['enc'] is None else u['enc'] + desloc
            atraso = None if (u['enc'] is None or h is None) else u['enc'] - h * 3600
            todas.append((uf, mun, zona, secao, enc, atraso, u))
            chave_local = secoes.get((uf, mun, zona, secao))
            if chave_local is None and u['local']:
                chave_local = (uf, mun, zona, u['local'])
            info = locais.get(chave_local) if chave_local else None
            if info is None:
                sem_local += 1
                info = {'n': f'Local {u["local"]} (zona {zona})' if u['local'] else f'Zona {zona}', 'e': '', 'b': '',
                        'c': '', 't': '', 'la': None, 'lo': None}
                chave_local = (uf, mun, zona, u['local'] or -zona)
            reg = locais_uf.get(chave_local)
            if reg is None:
                reg = locais_uf[chave_local] = {'m': mun, 'z': zona, 'l': chave_local[3], **info, 's': []}
            reg['s'].append([zona, secao, u['aptos'], u['comp'], u['v'][0], u['v'][1], u['b'], u['n'],
                             enc, u['agregadas'], tipos_uf.index(u['tipo'])])

        # Locais sem coordenada: no centro dos outros locais da mesma cidade (marcados como aproximados)
        centros = defaultdict(list)
        for reg in locais_uf.values():
            if reg['la'] is not None:
                centros[reg['m']].append((reg['la'], reg['lo']))
        for reg in locais_uf.values():
            if reg['la'] is None:
                sem_coord += len(reg['s'])
                pts = centros.get(reg['m'])
                if pts:
                    reg['la'] = round(statistics.median(p[0] for p in pts), 5)
                    reg['lo'] = round(statistics.median(p[1] for p in pts), 5)
                    reg['ap'] = 1

        nomes = {**{m: n for (u2, m), n in nomes_mun_locais.items() if u2 == uf}, **nomes_mun}
        saida_locais = []
        for reg in sorted(locais_uf.values(), key=lambda r: (r['m'], r['z'], r['l'])):
            item = {'m': reg['m'], 'n': reg['n'], 'e': reg['e'], 'b': reg['b'], 'c': reg['c'],
                    'la': reg['la'], 'lo': reg['lo'], 's': reg['s']}
            if reg.get('t'):
                item['t'] = reg['t']
            if reg.get('ap'):
                item['ap'] = 1
            saida_locais.append(item)
        arquivo = {
            'uf': uf,
            'versao': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'fonte': 'TSE, boletins de urna do 2º turno e eleitorado por local de votação (dados abertos)',
            'horario': 'local' if uf == 'zz' else 'brasilia',
            'candidatos': list(CANDIDATOS),
            # encerramento em segundos desde 0h (Brasília; no exterior, horário local); tipo = índice em tipos_urna
            'campos_secao': ['zona', 'secao', 'aptos', 'comparecimento', 'votos_13', 'votos_22', 'brancos',
                             'nulos', 'encerramento', 'agregadas', 'tipo'],
            'tipos_urna': tipos_uf,
            'municipios': {m: (nomes.get(m) or m) for m in sorted({r['m'] for r in locais_uf.values()})},
            'locais': saida_locais,
        }
        with open(os.path.join(SAIDA, f'{uf}.json'), 'w', encoding='utf-8') as f:
            json.dump(arquivo, f, ensure_ascii=False, separators=(',', ':'))
        tam = os.path.getsize(os.path.join(SAIDA, f'{uf}.json')) / 1e6
        log(f'{uf.upper()}: {len(urnas)} urnas em {len(locais_uf)} locais; hora oficial local {fusos[uf]}; {tam:.1f} MB')
        resumo_ufs[uf] = {'urnas': len(urnas), 'locais': len(locais_uf), 'arquivo_mb': round(tam, 2)}

    # ---------------- Totais e urnas encerradas depois do horário ----------------
    def soma(lista):
        t = {'urnas': 0, 'aptos': 0, 'comparecimento': 0, 'votos_13': 0, 'votos_22': 0, 'brancos': 0, 'nulos': 0}
        for *_, u in lista:
            t['urnas'] += 1
            t['aptos'] += u['aptos']
            t['comparecimento'] += u['comp']
            t['votos_13'] += u['v'][0]
            t['votos_22'] += u['v'][1]
            t['brancos'] += u['b']
            t['nulos'] += u['n']
        return t

    faixas = [  # segundos depois do horário oficial (17h00min00s)
        ('ate_17h00min59s', None, 59),
        ('17h01_a_17h04', 60, 4 * 60 + 59),
        ('17h05_a_17h14', 5 * 60, 14 * 60 + 59),
        ('17h15_a_17h29', 15 * 60, 29 * 60 + 59),
        ('17h30_a_17h59', 30 * 60, 59 * 60 + 59),
        ('18h00_a_18h59', 60 * 60, 119 * 60 + 59),
        ('19h_ou_depois', 120 * 60, None),
    ]
    com_horario = [x for x in todas if x[5] is not None]
    sem_horario = [x for x in todas if x[5] is None]
    dist = {}
    for nome, de, ate in faixas:
        grupo = [x for x in com_horario if (de is None or x[5] >= de) and (ate is None or x[5] <= ate)]
        dist[nome] = soma(grupo)

    # "depois_17h00": encerrada a partir de 17h00min01s; as outras, a partir do minuto indicado
    limites = {'depois_17h00': 1, 'depois_17h01': 60, 'depois_17h05': 5 * 60, 'depois_17h15': 15 * 60,
               'depois_17h30': 30 * 60, 'depois_18h00': 60 * 60}
    depois = {}
    for nome, lim in limites.items():
        grupo = [x for x in com_horario if x[5] >= lim]
        t = soma(grupo)
        t['por_uf'] = {uf: soma([x for x in grupo if x[0] == uf]) for uf in UFS if any(x[0] == uf for x in grupo)}
        depois[nome] = t

    minutos = Counter()
    for x in com_horario:
        m = x[5] // 60
        minutos[max(-30, min(m, 300))] += 1

    ultimas = sorted(com_horario, key=lambda x: -x[5])[:25]

    def descrever(x):
        uf, mun, zona, secao, enc, atraso, u = x
        chave_local = secoes.get((uf, mun, zona, secao)) or (uf, mun, zona, u['local'])
        info = locais.get(chave_local) or {}
        return {'uf': uf, 'municipio': mun, 'zona': zona, 'secao': secao,
                'encerramento': hhmm(enc), 'minutos_depois': round(atraso / 60, 1),
                'local': info.get('n', ''), 'endereco': info.get('e', ''), 'bairro': info.get('b', ''),
                'aptos': u['aptos'], 'comparecimento': u['comp'], 'votos_13': u['v'][0], 'votos_22': u['v'][1]}

    resumo = {
        'versao': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'fonte': 'TSE, boletins de urna do 2º turno (bweb_2t) e eleitorado por local de votação 2026',
        'nota': 'Horário de encerramento convertido para Brasília; no exterior, horário local. '
                '"depois_17h00" conta urnas encerradas a partir de 17h00min01s.',
        'candidatos': list(CANDIDATOS),
        'total': soma(todas),
        'sem_horario_de_encerramento': soma(sem_horario),
        'faixas_de_encerramento': dist,
        'depois_do_horario': depois,
        'urnas_por_minuto_depois_das_17h': {str(k): v for k, v in sorted(minutos.items())},
        'ultimas_a_fechar': [descrever(x) for x in ultimas],
        'hora_oficial_local_por_uf': fusos,
        'tipos_de_urna': dict(tipos_urna),
        'urnas_sem_local_identificado': sem_local,
        'urnas_em_locais_sem_coordenada': sem_coord,
        'estados': resumo_ufs,
    }
    with open(os.path.join(SAIDA, 'resumo.json'), 'w', encoding='utf-8') as f:
        json.dump(resumo, f, ensure_ascii=False, indent=1)

    # ---------------- Resumo no log ----------------
    tot = resumo['total']
    log(f'TOTAL: {tot["urnas"]} urnas | Lula {tot["votos_13"]} | Flávio {tot["votos_22"]} | '
        f'brancos {tot["brancos"]} | nulos {tot["nulos"]} | comparecimento {tot["comparecimento"]}')
    log(f'Cargos encontrados nos boletins: {dict(cargos_total)}')
    log(f'Tipos de urna: {dict(tipos_urna)}')
    log(f'Hora oficial local por UF: {fusos}')
    log(f'Sem horário de encerramento: {resumo["sem_horario_de_encerramento"]["urnas"]}')
    for nome, t in dist.items():
        log(f'  {nome:18s} {t["urnas"]:7d} urnas | Lula {t["votos_13"]:10d} | Flávio {t["votos_22"]:10d}')
    for nome, t in depois.items():
        log(f'  {nome:14s} {t["urnas"]:7d} urnas | Lula {t["votos_13"]:10d} | Flávio {t["votos_22"]:10d} | '
            f'comparec. {t["comparecimento"]}')
    log('Últimas a fechar: ' + '; '.join(f'{d["uf"].upper()} {d["municipio"]} z{d["zona"]} s{d["secao"]} '
                                          f'{d["encerramento"]}' for d in resumo['ultimas_a_fechar'][:8]))
    log(f'Urnas sem local identificado: {sem_local}; em locais sem coordenada: {sem_coord}')
    log(f'Fim em {time.time() - t0:.0f} s')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print('::error::' + str(e))
        sys.exit(1)
