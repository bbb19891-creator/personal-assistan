// WhatsApp Web bridge (Baileys) for agent.py — localhost only.
import makeWASocket, {
  useMultiFileAuthState, DisconnectReason, downloadMediaMessage,
  fetchLatestBaileysVersion, Browsers, jidNormalizedUser,
} from '@whiskeysockets/baileys';
import QRCode from 'qrcode';
import pino from 'pino';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = process.env.WA_BASE || path.dirname(fileURLToPath(import.meta.url));
const AUTH_DIR = process.env.WA_AUTH_DIR || path.join(BASE, 'auth');
const MEDIA_DIR = process.env.WA_MEDIA_DIR || path.join(BASE, 'media');
const QR_PNG = path.join(BASE, 'qr.png');
const STATUS_FILE = path.join(BASE, 'status.json');
const API_HOST = '127.0.0.1';
const API_PORT = parseInt(process.env.WA_BRIDGE_PORT || '8795', 10);
const INBOUND_URL = process.env.WA_INBOUND_URL || 'http://127.0.0.1:8794/wa/inbound';
const TOKEN = process.env.WA_BRIDGE_TOKEN || '';
fs.mkdirSync(AUTH_DIR, { recursive: true, mode: 0o700 });
fs.mkdirSync(MEDIA_DIR, { recursive: true });

const logger = pino({ level: process.env.WA_LOG_LEVEL || 'warn' });
const log = (...a) => console.log(new Date().toISOString(), ...a);

let sock = null;
let state = { connection: 'starting', me: null, lastQrAt: null, qrCount: 0, lastError: null, updatedAt: null };
const sentIds = new Set();          // ids we sent (avoid echo loops in self-chat)
const recent = new Map();           // id -> WAMessage (for quoting replies)
const groupNames = new Map();
const groupParts = new Map();       // jid -> [phone digits / lid ids]
function partsOf(meta) {
  return (meta?.participants || []).map(x => {
    const pn = x.phoneNumber || (String(x.id || '').endsWith('@s.whatsapp.net') ? x.id : '');
    return pn ? String(pn).split('@')[0].split(':')[0] : String(x.id || '');
  });
}
async function groupInfo(jid, force = false) {
  if (!force && groupNames.has(jid) && groupParts.has(jid)) return { subject: groupNames.get(jid), participants: groupParts.get(jid) };
  const meta = await sock.groupMetadata(jid);
  groupNames.set(jid, meta.subject || ''); groupParts.set(jid, partsOf(meta));
  return { subject: meta.subject || '', participants: groupParts.get(jid) };
}

function writeStatus(patch) {
  state = { ...state, ...patch, updatedAt: new Date().toISOString() };
  try { fs.writeFileSync(STATUS_FILE, JSON.stringify(state, null, 2)); } catch {}
}
function remember(m) {
  recent.set(m.key.id, m);
  if (recent.size > 1000) recent.delete(recent.keys().next().value);
}
function rememberSent(id) {
  if (!id) return; sentIds.add(id);
  if (sentIds.size > 2000) sentIds.delete(sentIds.values().next().value);
}

function unwrap(message) {
  let m = message || {};
  for (let i = 0; i < 5; i++) {
    const inner = m.ephemeralMessage?.message || m.viewOnceMessage?.message ||
      m.viewOnceMessageV2?.message || m.viewOnceMessageV2Extension?.message ||
      m.documentWithCaptionMessage?.message || m.editedMessage?.message;
    if (!inner) break; m = inner;
  }
  return m;
}
function extOf(mime, fallback) {
  if (!mime) return fallback;
  const t = mime.split(';')[0].trim();
  const map = { 'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'audio/ogg': 'ogg',
    'audio/mpeg': 'mp3', 'audio/mp4': 'm4a', 'application/pdf': 'pdf', 'video/mp4': 'mp4' };
  return map[t] || t.split('/')[1] || fallback;
}
function safeName(s) { return (s || '').replace(/[^\p{L}\p{N}._-]+/gu, '_').slice(0, 120); }

const SPOOL = path.join(BASE, 'inbound_spool.jsonl');
async function postOnce(payload) {
  const r = await fetch(INBOUND_URL, {
    method: 'POST', headers: { 'content-type': 'application/json', 'x-wa-token': TOKEN },
    body: JSON.stringify(payload), signal: AbortSignal.timeout(10000),
  });
  return r.ok;
}
async function postInbound(payload) {
  for (let attempt = 1; attempt <= 3; attempt++) {
    try { if (await postOnce(payload)) return true; log('[inbound] HTTP not ok'); }
    catch (e) { log('[inbound] post failed (attempt', attempt + ')', e.message); }
    await new Promise(r => setTimeout(r, 2000 * attempt));
  }
  fs.appendFileSync(SPOOL, JSON.stringify(payload) + '\n');   // never drop: spooled and retried every 5s
  log('[inbound] spooled message', payload.message_id || payload.type);
  return false;
}
let flushing = false;
setInterval(async () => {
  if (flushing || !fs.existsSync(SPOOL)) return;
  flushing = true;
  try {
    const tmp = SPOOL + '.flush'; fs.renameSync(SPOOL, tmp);
    const lines = fs.readFileSync(tmp, 'utf8').split('\n').filter(Boolean);
    let i = 0;
    for (; i < lines.length; i++) {
      try { if (!(await postOnce(JSON.parse(lines[i])))) break; } catch { break; }
    }
    if (i < lines.length) fs.appendFileSync(SPOOL, lines.slice(i).join('\n') + '\n');
    if (i) log(`[inbound] spool flushed ${i}/${lines.length}`);
    fs.unlinkSync(tmp);
  } catch (e) { log('[inbound] spool flush error', e.message); }
  flushing = false;
}, 5000);

function myIds() {
  const u = sock?.user || {};
  return [u.id, u.lid].filter(Boolean).map(j => jidNormalizedUser(j));
}

async function handleMessage(m) {
  if (!m.message || !m.key?.remoteJid) return;
  const chatId = m.key.remoteJid;
  if (chatId === 'status@broadcast' || chatId.endsWith('@newsletter') || chatId.endsWith('@broadcast')) return;
  const mine = myIds();
  if (m.key.fromMe) {
    // Only accept own messages in the "message yourself" chat, and never our own bot replies.
    if (sentIds.has(m.key.id)) return;
    if (!mine.includes(jidNormalizedUser(chatId))) return;
  }
  const msg = unwrap(m.message);
  const isGroup = chatId.endsWith('@g.us');
  const sender = isGroup ? (m.key.participant || '') : (m.key.fromMe ? (mine[0] || chatId) : chatId);
  const senderAlt = isGroup ? (m.key.participantAlt || '') : (m.key.remoteJidAlt || '');
  const pnJid = [sender, senderAlt].find(j => j && j.endsWith('@s.whatsapp.net')) || '';

  let type = null, text = '', mime = '', fileName = '', ctx = null;
  if (msg.conversation) { type = 'text'; text = msg.conversation; }
  else if (msg.extendedTextMessage) { type = 'text'; text = msg.extendedTextMessage.text || ''; ctx = msg.extendedTextMessage.contextInfo; }
  else if (msg.imageMessage) { type = 'image'; text = msg.imageMessage.caption || ''; mime = msg.imageMessage.mimetype; ctx = msg.imageMessage.contextInfo; }
  else if (msg.documentMessage) { type = 'document'; text = msg.documentMessage.caption || ''; mime = msg.documentMessage.mimetype; fileName = msg.documentMessage.fileName || ''; ctx = msg.documentMessage.contextInfo; }
  else if (msg.audioMessage) { type = msg.audioMessage.ptt ? 'voice' : 'audio'; mime = msg.audioMessage.mimetype; ctx = msg.audioMessage.contextInfo; }
  else return; // stickers, reactions, protocol msgs, etc. ignored

  remember(m);
  const mentioned = (ctx?.mentionedJid || []).map(j => jidNormalizedUser(j));
  const quotedParticipant = ctx?.participant ? jidNormalizedUser(ctx.participant) : '';
  const mentionedBot = mentioned.some(j => mine.includes(j));
  const replyToBot = !!(ctx?.stanzaId && (sentIds.has(ctx.stanzaId) || (quotedParticipant && mine.includes(quotedParticipant))));

  let mediaPath = null;
  if (type !== 'text') {
    try {
      const buf = await downloadMediaMessage(m, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
      const ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
      const base = fileName ? safeName(fileName) : `${type}.${extOf(mime, 'bin')}`;
      mediaPath = path.join(MEDIA_DIR, `${ts}_${safeName(m.key.id)}_${base}`);
      fs.writeFileSync(mediaPath, buf);
    } catch (e) { log('[media] download failed', e.message); }
  }

  let groupName = '', groupParticipants = [];
  if (isGroup) {
    try { const gi = await groupInfo(chatId); groupName = gi.subject; groupParticipants = gi.participants; } catch {}
  }

  const payload = {
    message_id: m.key.id, chat_id: chatId, is_group: isGroup, group_name: groupName, group_participants: groupParticipants,
    quoted_id: ctx?.stanzaId || null,
    sender, sender_alt: senderAlt, sender_pn: pnJid ? pnJid.split('@')[0] : '',
    sender_name: m.pushName || '', from_me: !!m.key.fromMe,
    type, text, mimetype: mime || null, file_name: fileName || null, media_path: mediaPath,
    mentioned_bot: mentionedBot, reply_to_bot: replyToBot, mentioned_jids: mentioned,
    quoted_text: ctx?.quotedMessage ? (unwrap(ctx.quotedMessage).conversation || unwrap(ctx.quotedMessage).extendedTextMessage?.text || '') : '',
    bot_jids: mine, timestamp: Number(m.messageTimestamp || 0),
  };
  log(`[in] ${isGroup ? 'group' : 'pm'} chat=${chatId} from=${sender}${pnJid ? ' pn=' + payload.sender_pn : ''} name="${payload.sender_name}" type=${type} len=${text.length}${mediaPath ? ' media=' + mediaPath : ''}`);
  await postInbound(payload);
}

async function start() {
  const { state: auth, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  let version;
  try { ({ version } = await fetchLatestBaileysVersion()); } catch {}
  sock = makeWASocket({
    auth, logger, version, browser: Browsers.ubuntu('Chrome'),
    markOnlineOnConnect: false, syncFullHistory: false, generateHighQualityLinkPreview: false,
  });
  sock.ev.on('creds.update', saveCreds);
  sock.ev.on('connection.update', async (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      try {
        await QRCode.toFile(QR_PNG + '.tmp.png', qr, { type: 'png', width: 512, margin: 2, errorCorrectionLevel: 'M' });
        fs.renameSync(QR_PNG + '.tmp.png', QR_PNG);
        writeStatus({ connection: 'awaiting_qr_scan', lastQrAt: new Date().toISOString(), qrCount: state.qrCount + 1 });
        log(`[qr] new QR written to ${QR_PNG} (#${state.qrCount})`);
      } catch (e) { log('[qr] write failed', e.message); }
    }
    if (connection === 'open') {
      try { fs.unlinkSync(QR_PNG); } catch {}
      writeStatus({ connection: 'open', me: sock.user || null, lastError: null, qrCount: 0 });
      log('[conn] OPEN as', sock.user?.id, sock.user?.lid || '', sock.user?.name || '');
    } else if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode;
      const reason = lastDisconnect?.error?.message || String(code);
      writeStatus({ connection: 'closed', lastError: `${code} ${reason}` });
      log('[conn] CLOSED code=', code, reason);
      if (code === DisconnectReason.loggedOut) {
        log('[conn] logged out — wiping auth state, a new QR will be generated');
        try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); fs.mkdirSync(AUTH_DIR, { recursive: true, mode: 0o700 }); } catch {}
      }
      const delay = code === DisconnectReason.restartRequired ? 500 : 3000;
      setTimeout(() => start().catch(e => { log('[conn] restart failed', e.message); process.exit(1); }), delay);
    } else if (connection === 'connecting') {
      writeStatus({ connection: 'connecting' });
      log('[conn] connecting...');
    }
  });
  const groupEvent = async (jid, why) => {
    try {
      const gi = await groupInfo(jid, true);
      log(`[group] ${why} ${jid} "${gi.subject}" members=${gi.participants.length}`);
      await postInbound({ type: 'group_event', event: why, chat_id: jid, is_group: true, group_name: gi.subject,
        group_participants: gi.participants, bot_jids: myIds(), timestamp: Math.floor(Date.now() / 1000) });
    } catch (e) { log('[group] event error', e.message); }
  };
  sock.ev.on('groups.upsert', async (metas) => { for (const g of metas) await groupEvent(g.id, 'upsert'); });
  sock.ev.on('group-participants.update', async (u) => { await groupEvent(u.id, 'participants:' + u.action); });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const m of messages) {
      try { await handleMessage(m); } catch (e) { log('[in] handler error', e.message); }
    }
  });
}

// ---------- localhost send API ----------
function readJson(req) {
  return new Promise((resolve, reject) => {
    let b = ''; req.on('data', c => { b += c; if (b.length > 2e6) req.destroy(); });
    req.on('end', () => { try { resolve(b ? JSON.parse(b) : {}); } catch (e) { reject(e); } });
  });
}
function send(res, code, obj) { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(obj)); }

http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url === '/status') return send(res, 200, state);
    if (TOKEN && req.headers['x-wa-token'] !== TOKEN) return send(res, 401, { ok: false, error: 'bad token' });
    if (req.method === 'GET' && req.url === '/qr.png') {
      if (!fs.existsSync(QR_PNG)) return send(res, 404, { ok: false, error: 'no qr (linked?)' });
      res.writeHead(200, { 'content-type': 'image/png' }); return fs.createReadStream(QR_PNG).pipe(res);
    }
    if (req.method === 'GET' && req.url === '/groups') {
      if (!sock || state.connection !== 'open') return send(res, 503, { ok: false, error: 'not connected' });
      const all = await sock.groupFetchAllParticipating();
      const groups = Object.values(all).map(g => {
        groupNames.set(g.id, g.subject || ''); groupParts.set(g.id, partsOf(g));
        return { jid: g.id, subject: g.subject || '', participants: groupParts.get(g.id) };
      });
      return send(res, 200, { ok: true, groups, bot_jids: myIds() });
    }
    if (req.method === 'POST' && req.url === '/pair') {
      // Link with phone number: returns an 8-char code the owner types in WhatsApp > Linked devices > Link with phone number.
      const body = await readJson(req);
      const digits = String(body.phone || '').replace(/\D/g, '');
      if (digits.length < 8) return send(res, 400, { ok: false, error: 'phone (international digits) required' });
      if (!sock) return send(res, 503, { ok: false, error: 'socket not ready' });
      if (state.connection === 'open') return send(res, 409, { ok: false, error: 'already linked' });
      const code = await sock.requestPairingCode(digits);
      writeStatus({ connection: 'awaiting_pairing_code', lastQrAt: new Date().toISOString() });
      log('[pair] pairing code issued');
      return send(res, 200, { ok: true, code });
    }
    if (req.method === 'POST' && req.url === '/logout') {
      try { if (sock) await sock.logout(); } catch (e) { log('[logout]', e.message); }
      try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); fs.mkdirSync(AUTH_DIR, { recursive: true, mode: 0o700 }); } catch {}
      writeStatus({ connection: 'closed', me: null, lastError: 'logged out by owner' });
      setTimeout(() => process.exit(0), 300);   // systemd restarts the bridge -> fresh QR / pairing
      return send(res, 200, { ok: true });
    }
    if (req.method !== 'POST') return send(res, 404, { ok: false, error: 'not found' });
    if (!sock || state.connection !== 'open') return send(res, 503, { ok: false, error: 'not connected: ' + state.connection });
    const body = await readJson(req);
    const jid = body.chat_id;
    if (!jid) return send(res, 400, { ok: false, error: 'chat_id required' });
    const quoted = body.quoted_id ? recent.get(body.quoted_id) : undefined;
    let content;
    if (req.url === '/send/text') {
      if (!body.text) return send(res, 400, { ok: false, error: 'text required' });
      content = { text: String(body.text) };
    } else if (req.url === '/send/file') {
      const p = body.path;
      if (!p || !fs.existsSync(p)) return send(res, 400, { ok: false, error: 'path missing' });
      const buf = fs.readFileSync(p);
      const fileName = body.filename || path.basename(p);
      const mt = body.mimetype || (/\.pdf$/i.test(p) ? 'application/pdf' : /\.(jpe?g)$/i.test(p) ? 'image/jpeg' :
        /\.png$/i.test(p) ? 'image/png' : /\.docx$/i.test(p) ? 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' : 'application/octet-stream');
      if (body.as_image && mt.startsWith('image/')) content = { image: buf, caption: body.caption || undefined, mimetype: mt };
      else content = { document: buf, fileName, mimetype: mt, caption: body.caption || undefined };
    } else return send(res, 404, { ok: false, error: 'not found' });
    const r = await sock.sendMessage(jid, content, quoted ? { quoted } : {});
    rememberSent(r?.key?.id);
    if (r) remember(r);
    log(`[out] ${req.url} chat=${jid} id=${r?.key?.id}`);
    return send(res, 200, { ok: true, id: r?.key?.id });
  } catch (e) {
    log('[api] error', e.message);
    return send(res, 500, { ok: false, error: e.message });
  }
}).listen(API_PORT, API_HOST, () => log(`[api] listening on http://${API_HOST}:${API_PORT}`));

writeStatus({ connection: 'starting' });
start().catch(e => { log('[fatal]', e); process.exit(1); });
