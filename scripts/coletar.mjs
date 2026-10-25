#!/usr/bin/env node
/**
 * Coletor de resultados do TSE — Eleições 2026, Presidente (1º e 2º turnos).
 *
 * Baixa os arquivos JSON oficiais da divulgação de resultados
 * (https://resultados.tse.jus.br) e gera três arquivos para o site:
 *
 *   dados/resumo.json      Brasil e estados, 1º e 2º turnos
 *   dados/municipios.json  todas as cidades (e o exterior), 1º e 2º turnos
 *   dados/sincronia.json   controle interno: o que já foi baixado e quando
 *
 * Feito para rodar no GitHub Actions a cada 5 minutos, mas funciona em
 * qualquer computador com Node 18 ou mais novo:
 *
 *   node scripts/coletar.mjs
 *
 * Regras do TSE que o script respeita:
 *   - no máximo 100 requisições por segundo por IP (usamos 30 por padrão);
 *   - vários erros 404 podem bloquear o IP, então só pedimos arquivos que o
 *     próprio TSE lista nos arquivos de configuração e de acompanhamento;
 *   - se o TSE bloquear (HTTP 403/429), o script para e espera 12 minutos
 *     antes de tentar de novo (tentar antes reinicia o bloqueio).
 *
 * Só baixa de novo o que mudou: os arquivos de acompanhamento do TSE dizem
 * a hora da última totalização de cada estado e de cada cidade.
 */

import fs from 'node:fs/promises';
import path from 'node:path';

const env = process.env;

const CFG = {
  base: (env.TSE_BASE || 'https://resultados.tse.jus.br').replace(/\/+$/, ''),
  ambiente: env.TSE_AMBIENTE || 'oficial',
  ciclo: env.TSE_CICLO || 'ele2026',
  // Códigos publicados pelo TSE para 2026: 6257 = 1º turno, 6258 = 2º turno
  eleicoes: { t1: env.TSE_ELEICAO_T1 || '6257', t2: env.TSE_ELEICAO_T2 || '6258' },
  cargo: env.TSE_CARGO || '1', // 1 = Presidente
  // Ordem fixa de exibição (pelo número na urna): 13 = Lula, 22 = Flávio Bolsonaro
  candidatos: (env.CANDIDATOS || '13,22').split(',').map(s => s.trim()).filter(Boolean),
  porSegundo: Number(env.REQ_POR_SEGUNDO || 30),
  paralelas: Number(env.REQ_PARALELAS || 12),
  tempoMaximoMin: Number(env.TEMPO_MAXIMO_MIN || 9),
  pasta: env.PASTA_DADOS || 'dados',
  esperaBloqueioMin: 12,
};

const TURNOS = ['t2', 't1']; // o 2º turno primeiro: é o que está mudando

// ---------------------------------------------------------------------------
// Utilidades

const sleep = ms => new Promise(r => setTimeout(r, ms));
const pad = (v, n) => String(v).padStart(n, '0');
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const aviso = msg => console.log(`::warning::${msg}`);

function inteiro(v) {
  if (v === undefined || v === null || v === '') return null;
  const n = Number.parseInt(String(v), 10);
  return Number.isFinite(n) ? n : null;
}

// O TSE usa vírgula como separador decimal ("45,16")
function decimal(v) {
  if (v === undefined || v === null || v === '') return null;
  const s = String(v).trim();
  const n = Number(s.includes(',') ? s.replace(/\./g, '').replace(',', '.') : s);
  return Number.isFinite(n) ? n : null;
}

// "25/10/2026" + "20:41:12" -> "2026-10-25 20:41:12" (ordenável como texto)
function carimbo(dt, ht) {
  if (!dt) return '';
  const [d, m, a] = String(dt).split('/');
  if (!a) return '';
  return `${a}-${pad(m, 2)}-${pad(d, 2)} ${ht || '00:00:00'}`;
}

const ENTIDADES = { amp: '&', apos: "'", quot: '"', lt: '<', gt: '>', nbsp: ' ' };
function decodificar(s) {
  return String(s ?? '').replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
    if (e[0] === '#') {
      const c = e[1].toLowerCase() === 'x' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return Number.isFinite(c) ? String.fromCodePoint(c) : m;
    }
    return ENTIDADES[e.toLowerCase()] ?? m;
  }).trim();
}

const arquivo = nome => path.join(CFG.pasta, nome);

async function lerJSON(caminho) {
  try { return JSON.parse(await fs.readFile(caminho, 'utf8')); } catch { return null; }
}

async function gravarJSON(caminho, dados) {
  await fs.mkdir(path.dirname(caminho), { recursive: true });
  const tmp = `${caminho}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(dados));
  await fs.rename(tmp, caminho);
}

// ---------------------------------------------------------------------------
// Endereços dos arquivos do TSE

const raiz = () => `${CFG.base}/${CFG.ambiente}/${CFG.ciclo}`;
const e6 = ele => `e${pad(ele, 6)}`;
const c4 = () => `c${pad(CFG.cargo, 4)}`;

const URL_TSE = {
  // EA12 – lista de municípios
  municipios: ele => `${raiz()}/${ele}/config/mun-${e6(ele)}-cm.json`,
  // EA14 (uf = "br") e EA15 – acompanhamento da totalização
  acompanhamento: (ele, uf) => `${raiz()}/${ele}/dados/${uf}/${uf}-${e6(ele)}-ab.json`,
  // EA20 – resultado unificado (Brasil, estado ou município)
  resultado: (ele, uf, mun = '') => `${raiz()}/${ele}/dados/${uf}/${uf}${mun}-${c4()}-${e6(ele)}-u.json`,
};

// ---------------------------------------------------------------------------
// Download com limite de ritmo, novas tentativas e detecção de bloqueio

class Bloqueio extends Error {}

const stats = { requisicoes: 0, ok: 0, nao_encontrados: 0, erros: 0, bytes: 0 };
let interrompido = null; // motivo, se o TSE bloquear no meio do caminho
let proximaVaga = 0;

async function aguardarVaga() {
  const intervalo = 1000 / CFG.porSegundo;
  const t = Date.now();
  const minha = Math.max(t, proximaVaga);
  proximaVaga = minha + intervalo;
  if (minha > t) await sleep(minha - t);
}

async function baixarJSON(url, { pode404 = false } = {}) {
  for (let tentativa = 1; ; tentativa++) {
    if (interrompido) throw new Bloqueio(interrompido);
    await aguardarVaga();
    stats.requisicoes++;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 25_000);
    try {
      const r = await fetch(url, {
        signal: ctrl.signal,
        headers: {
          accept: 'application/json, text/plain, */*',
          'accept-language': 'pt-BR,pt;q=0.9',
          'user-agent': 'Mozilla/5.0 (compatible; apuracao-2026/1.0; site independente de divulgacao de resultados)',
        },
      });
      if (r.status === 404) {
        stats.nao_encontrados++;
        if (pode404) return null;
        const e = new Error(`Arquivo não encontrado (404): ${url}`);
        e.semNovaTentativa = true;
        throw e;
      }
      if (r.status === 403 || r.status === 429) {
        interrompido = `HTTP ${r.status} em ${url}`;
        throw new Bloqueio(interrompido);
      }
      if (!r.ok) throw new Error(`HTTP ${r.status} em ${url}`);
      const texto = await r.text();
      stats.bytes += texto.length;
      const json = JSON.parse(texto);
      stats.ok++;
      return json;
    } catch (e) {
      if (e instanceof Bloqueio || e.semNovaTentativa) throw e;
      stats.erros++;
      if (tentativa >= 3) throw e;
      await sleep(800 * tentativa + Math.random() * 400);
    } finally {
      clearTimeout(timer);
    }
  }
}

async function emParalelo(itens, quantas, tarefa) {
  let i = 0;
  const trabalhador = async () => {
    while (i < itens.length && !interrompido) {
      const item = itens[i++];
      try {
        await tarefa(item);
      } catch (e) {
        if (e instanceof Bloqueio) return;
        stats.falhas = (stats.falhas || 0) + 1;
        if (stats.falhas <= 5) log('  falhou:', e.message);
      }
    }
  };
  await Promise.all(Array.from({ length: Math.max(1, Math.min(quantas, itens.length)) }, trabalhador));
}

// ---------------------------------------------------------------------------
// Leitura dos arquivos do TSE

function lerResultado(j) {
  if (!j || typeof j !== 'object') return null;
  const cargos = Array.isArray(j.carg) ? j.carg : [];
  const cargo = cargos.find(c => String(c.cd) === String(CFG.cargo)) || cargos[0];
  const cand = {};
  for (const agr of cargo?.agr || []) {
    for (const par of agr.par || []) {
      for (const c of par.cand || []) {
        cand[String(c.n)] = {
          v: inteiro(c.vap) ?? 0,
          p: decimal(c.pvapn ?? c.pvap),
          e: c.e === 's' ? 's' : 'n',
          st: decodificar(c.st),
          nmu: decodificar(c.nmu || c.nm),
          nm: decodificar(c.nm),
          partido: decodificar(par.sg),
          agremiacao: decodificar(agr.nm),
          vice: decodificar(c.vs?.[0]?.nmu || c.vs?.[0]?.nm || ''),
        };
      }
    }
  }
  const s = j.s || {};
  const el = j.e || {};
  const v = j.v || {};
  return {
    and: j.and || '',
    dt: carimbo(j.dt, j.ht),
    dg: carimbo(j.dg, j.hg),
    md: j.md || '',
    tf: j.tf || '',
    secoes: inteiro(s.ts),
    secoesTot: inteiro(s.st),
    pst: decimal(s.pstn ?? s.pst),
    te: inteiro(el.te),
    c: inteiro(el.c),
    a: inteiro(el.a),
    pc: decimal(el.pcn ?? el.pc), // % de comparecimento sobre o eleitorado já apurado
    pa: decimal(el.pan ?? el.pa), // % de abstenção sobre o eleitorado já apurado
    vv: inteiro(v.vv),
    vb: inteiro(v.vb),
    vn: inteiro(v.tvn ?? v.vn),
    cand,
  };
}

// Versão enxuta para estados e Brasil (só os dois candidatos do 2º turno)
function enxugarUF(r) {
  if (!r) return null;
  const cand = {};
  for (const n of CFG.candidatos) {
    const c = r.cand?.[n];
    cand[n] = c ? { v: c.v ?? 0, p: c.p ?? null, e: c.e || 'n', st: c.st || '' } : { v: 0, p: null, e: 'n', st: '' };
  }
  const { and, dt, dg, md, tf, secoes, secoesTot, pst, te, c, a, pc, pa, vv, vb, vn } = r;
  return { and, dt, dg, md, tf, secoes, secoesTot, pst, te, c, a, pc, pa, vv, vb, vn, cand };
}

// Versão enxuta para municípios
function enxugarMunicipio(r) {
  return {
    and: r.and,
    dt: r.dt,
    pst: r.pst,
    te: r.te,
    c: r.c,
    vv: r.vv,
    v: CFG.candidatos.map(n => r.cand?.[n]?.v ?? 0),
  };
}

function lerListaMunicipios(j) {
  const lista = [];
  for (const uf of j?.abr || []) {
    const sigla = String(uf.cd || '').toLowerCase();
    for (const m of uf.mu || []) {
      const cd = pad(String(m.cd || '').trim(), 5); // o TSE exige 5 dígitos
      if (!/^\d{5}$/.test(cd)) continue;
      lista.push({ uf: sigla, cd, nome: decodificar(m.nm), capital: m.c === 's' ? 1 : 0, cdi: String(m.cdi || '') });
    }
  }
  return lista;
}

function lerAcompanhamento(j) {
  const mapa = new Map();
  for (const a of j?.abr || []) {
    mapa.set(`${a.tpabr}:${String(a.cdabr).toLowerCase()}`, {
      and: a.and || '',
      ts: carimbo(a.dt, a.ht),
      pst: decimal(a.s?.pstn ?? a.s?.pst),
      te: inteiro(a.e?.te),
    });
  }
  return mapa;
}

// ---------------------------------------------------------------------------
// Estado salvo da rodada anterior

async function carregarEstado() {
  const resumo = await lerJSON(arquivo('resumo.json'));
  const muni = await lerJSON(arquivo('municipios.json'));
  const sinc = await lerJSON(arquivo('sincronia.json'));
  const estado = { lista: [], candidatos: resumo?.candidatos || null };
  const mesmaConfig = resumo?.tse?.eleicoes?.t1 === CFG.eleicoes.t1 && resumo?.tse?.eleicoes?.t2 === CFG.eleicoes.t2
    && String(muni?.candidatos) === String(CFG.candidatos);

  for (const t of ['t1', 't2']) {
    estado[t] = { ele: CFG.eleicoes[t], br: null, uf: {}, mun: {}, completo: false };
  }
  if (!mesmaConfig) return estado;

  for (const t of ['t1', 't2']) {
    const est = estado[t];
    const rt = resumo?.turnos?.[t];
    const st = sinc?.[t] || {};
    est.br = rt?.br || null;
    est.completo = !!st.completo;
    for (const [uf, r] of Object.entries(rt?.uf || {})) {
      const [ab = '', pend = 0] = st.uf?.[uf] || [];
      est.uf[uf] = { r, ab, pend: !!pend };
    }
  }

  // Linhas: uf, cd, nome, capital, cdi, eleitorado, [t1: and pst c vv A B], [t2: and pst c vv A B]
  for (const l of muni?.linhas || []) {
    const [uf, cd, nome, capital, cdi, te] = l;
    estado.lista.push({ uf, cd, nome, capital, cdi });
    const chave = uf + cd;
    ['t1', 't2'].forEach((t, k) => {
      const o = 6 + k * 6;
      const [and, pst, c, vv, A, B] = l.slice(o, o + 6);
      const [ab = '', pend = 0, dt = ''] = sinc?.[t]?.mun?.[chave] || [];
      const temDado = and !== null && and !== undefined && and !== '';
      estado[t].mun[chave] = {
        r: temDado ? { and, dt, pst, te, c, vv, v: [A, B] } : null,
        ab,
        pend: !!pend,
      };
    });
  }
  return estado;
}

// ---------------------------------------------------------------------------
// Coleta de um turno

async function coletarTurno(t, est, lista, limite) {
  const ele = est.ele;
  const rotulo = t === 't1' ? '1º turno' : '2º turno';

  if (est.completo && est.br?.and === 'f') {
    log(`${rotulo}: totalização final já baixada, nada a fazer.`);
    return { baixados: 0 };
  }

  // 1. Acompanhamento do Brasil: quais estados mudaram
  const ab14 = await baixarJSON(URL_TSE.acompanhamento(ele, 'br'), { pode404: true });
  const acompBR = ab14 ? lerAcompanhamento(ab14) : null;
  if (!ab14) log(`${rotulo}: acompanhamento nacional indisponível; conferindo todos os estados.`);

  // 2. Resultado do Brasil
  const jBR = await baixarJSON(URL_TSE.resultado(ele, 'br'), { pode404: true });
  if (jBR) {
    const r = lerResultado(jBR);
    est.br = enxugarUF(r);
    est.brCompleto = r; // guarda nomes e partidos dos candidatos
  }

  // 3. Estados que mudaram (ou que têm cidade pendente)
  const ufs = [...new Set(lista.map(m => m.uf))];
  const ufsComPendencia = new Set(
    Object.entries(est.mun).filter(([, m]) => m.pend).map(([chave]) => chave.slice(0, 2)),
  );
  const mudaram = ufs.filter(uf => {
    const prev = est.uf[uf];
    if (!prev?.r) return true;
    if (ufsComPendencia.has(uf)) return true;
    if (!acompBR) return true;
    const s = acompBR.get(`uf:${uf}`);
    if (!s) return false;
    return prev.ab !== s.ts || prev.pend;
  });

  const acompMun = new Map();
  await emParalelo(mudaram, 4, async uf => {
    const s = acompBR?.get(`uf:${uf}`);
    const jUF = await baixarJSON(URL_TSE.resultado(ele, uf), { pode404: true });
    if (jUF) {
      const r = enxugarUF(lerResultado(jUF));
      const pend = !!(s && s.and !== 'n' && r.and !== 'f' && r.dt < s.ts);
      est.uf[uf] = { r, ab: s?.ts || '', pend };
    }
    const ab15 = await baixarJSON(URL_TSE.acompanhamento(ele, uf), { pode404: true });
    if (ab15) {
      for (const [chave, item] of lerAcompanhamento(ab15)) {
        if (chave.startsWith('mun:')) acompMun.set(uf + pad(chave.slice(4), 5), item);
      }
    }
  });

  // 4. Cidades que mudaram desde a última rodada
  const fila = [];
  for (const m of lista) {
    const chave = m.uf + m.cd;
    const s = acompMun.get(chave);
    if (!s) continue; // estado sem mudança, ou cidade fora do acompanhamento
    const prev = est.mun[chave];
    if (s.and === 'n' || !s.ts) { // apuração ainda não começou nessa cidade
      if (!prev) est.mun[chave] = { r: null, ab: '', pend: false };
      continue;
    }
    if (prev?.r && prev.ab === s.ts && !prev.pend) continue;
    fila.push({ m, chave, s });
  }
  // Maiores eleitorados primeiro
  fila.sort((a, b) => (b.s.te || 0) - (a.s.te || 0));

  let baixados = 0;
  let cortados = 0;
  await emParalelo(fila, CFG.paralelas, async ({ m, chave, s }) => {
    if (Date.now() > limite) { cortados++; return; }
    const j = await baixarJSON(URL_TSE.resultado(ele, m.uf, m.cd), { pode404: true });
    if (!j) { // não existe: não insiste até o acompanhamento mudar
      est.mun[chave] = { r: est.mun[chave]?.r || null, ab: s.ts, pend: false };
      return;
    }
    const r = enxugarMunicipio(lerResultado(j));
    est.mun[chave] = { r, ab: s.ts, pend: r.and !== 'f' && r.dt < s.ts };
    baixados++;
  });

  est.completo = est.br?.and === 'f' && lista.every(m => est.mun[m.uf + m.cd]?.r?.and === 'f');
  log(`${rotulo}: ${mudaram.length} estado(s) conferido(s), ${baixados} cidade(s) atualizada(s)` +
    (cortados ? `, ${cortados} ficaram para a próxima rodada (tempo esgotado)` : '') +
    (est.completo ? ' — totalização final completa.' : '.'));
  return { baixados, cortados };
}

// ---------------------------------------------------------------------------
// Gravação dos arquivos do site

function montarCandidatos(estado) {
  const fonte = estado.t2.brCompleto || estado.t1.brCompleto;
  if (!fonte) return estado.candidatos;
  return CFG.candidatos.map(n => {
    const c = fonte.cand[n] || {};
    return {
      n,
      nomeUrna: c.nmu || '',
      nome: c.nm || '',
      partido: c.partido || '',
      coligacao: c.agremiacao || '',
      vice: c.vice || '',
    };
  });
}

async function salvar(estado, info) {
  const versao = new Date().toISOString();
  const turnos = {};
  const sinc = {};
  for (const t of ['t1', 't2']) {
    const est = estado[t];
    turnos[t] = {
      eleicao: est.ele,
      completo: est.completo,
      br: est.br,
      uf: Object.fromEntries(Object.entries(est.uf).filter(([, v]) => v.r).map(([uf, v]) => [uf, v.r])),
    };
    sinc[t] = {
      completo: est.completo,
      uf: Object.fromEntries(Object.entries(est.uf).map(([uf, v]) => [uf, [v.ab || '', v.pend ? 1 : 0]])),
      mun: Object.fromEntries(Object.entries(est.mun).map(([k, v]) => [k, [v.ab || '', v.pend ? 1 : 0, v.r?.dt || '']])),
    };
  }

  const colunasTurno = m => m?.r
    ? [m.r.and || '', m.r.pst ?? null, m.r.c ?? null, m.r.vv ?? null, m.r.v?.[0] ?? null, m.r.v?.[1] ?? null]
    : [null, null, null, null, null, null];

  const linhas = estado.lista
    .slice()
    .sort((a, b) => a.uf.localeCompare(b.uf) || a.nome.localeCompare(b.nome, 'pt-BR'))
    .map(m => {
      const chave = m.uf + m.cd;
      const m1 = estado.t1.mun[chave];
      const m2 = estado.t2.mun[chave];
      const te = m2?.r?.te ?? m1?.r?.te ?? null;
      return [m.uf, m.cd, m.nome, m.capital, m.cdi, te, ...colunasTurno(m1), ...colunasTurno(m2)];
    });

  await gravarJSON(arquivo('resumo.json'), {
    versao,
    tse: {
      base: CFG.base, ambiente: CFG.ambiente, ciclo: CFG.ciclo, eleicoes: CFG.eleicoes, cargo: CFG.cargo,
    },
    candidatos: montarCandidatos(estado),
    coleta: info,
    turnos,
  });
  await gravarJSON(arquivo('municipios.json'), {
    versao,
    candidatos: CFG.candidatos,
    campos: ['uf', 'cd', 'nome', 'capital', 'cdi', 'eleitorado',
      't1_and', 't1_pst', 't1_comparecimento', 't1_validos', 't1_a', 't1_b',
      't2_and', 't2_pst', 't2_comparecimento', 't2_validos', 't2_a', 't2_b'],
    linhas,
  });
  await gravarJSON(arquivo('sincronia.json'), sinc);
}

// ---------------------------------------------------------------------------

async function main() {
  const inicio = Date.now();
  const limite = inicio + CFG.tempoMaximoMin * 60_000;
  log(`Coletando ${CFG.base}/${CFG.ambiente}/${CFG.ciclo} — eleições ${CFG.eleicoes.t1} e ${CFG.eleicoes.t2}`);

  const bloqueio = await lerJSON(arquivo('bloqueio.json'));
  if (bloqueio?.ate && Date.now() < Date.parse(bloqueio.ate)) {
    aviso(`O TSE bloqueou este endereço há pouco. Nova tentativa depois de ${bloqueio.ate}.`);
    return;
  }

  const estado = await carregarEstado();

  // Lista de cidades (configuração do TSE), se ainda não temos
  if (!estado.lista.length) {
    let cfg = await baixarJSON(URL_TSE.municipios(CFG.eleicoes.t2), { pode404: true });
    if (!cfg) cfg = await baixarJSON(URL_TSE.municipios(CFG.eleicoes.t1));
    estado.lista = lerListaMunicipios(cfg);
    log(`Lista de cidades: ${estado.lista.length} (incluindo o exterior).`);
    if (!estado.lista.length) throw new Error('A lista de cidades do TSE veio vazia.');
  }

  const resultado = {};
  let falhou = false;
  for (const t of TURNOS) {
    try {
      resultado[t] = await coletarTurno(t, estado[t], estado.lista, limite);
    } catch (e) {
      if (e instanceof Bloqueio) break;
      // Falha de rede persistente: registra, segue para o outro turno e salva o que tiver
      falhou = true;
      aviso(`Não consegui atualizar o ${t === 't1' ? '1º' : '2º'} turno agora: ${e.message}`);
    }
  }

  const info = {
    inicio: new Date(inicio).toISOString(),
    fim: new Date().toISOString(),
    requisicoes: stats.requisicoes,
    erros: stats.erros,
    bloqueado: !!interrompido,
    incompleto: !!interrompido || falhou || Object.values(resultado).some(r => r?.cortados),
  };
  await salvar(estado, info);

  if (interrompido) {
    const ate = new Date(Date.now() + CFG.esperaBloqueioMin * 60_000).toISOString();
    await gravarJSON(arquivo('bloqueio.json'), { motivo: interrompido, ate });
    aviso(`O TSE recusou as requisições (${interrompido}). Dados parciais salvos; nova tentativa depois de ${ate}.`);
  } else {
    await fs.rm(arquivo('bloqueio.json'), { force: true });
  }

  const kb = (stats.bytes / 1024).toFixed(0);
  log(`Fim: ${stats.requisicoes} requisições (${stats.nao_encontrados} não encontradas, ${stats.erros} erros), ${kb} KB, ${((Date.now() - inicio) / 1000).toFixed(1)} s.`);
}

main().catch(e => {
  console.error(e);
  process.exitCode = 1;
});
