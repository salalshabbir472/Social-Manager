// Google Drive + Sheets layer. Scope drive.file = app only touches files it creates.
const CID = import.meta.env.VITE_GOOGLE_CLIENT_ID
const D = 'https://www.googleapis.com/drive/v3', S = 'https://sheets.googleapis.com/v4/spreadsheets'
export const FOLDER = 'application/vnd.google-apps.folder', SHEET = 'application/vnd.google-apps.spreadsheet'
let token = null
export const getToken = () => token
const enc = encodeURIComponent
const now = () => new Date().toISOString().replace('T', ' ').slice(0, 19)

export function signIn() {
  return new Promise((res, rej) => {
    if (!window.google) return rej(new Error('Google script not loaded yet'))
    window.google.accounts.oauth2.initTokenClient({
      client_id: CID, scope: 'https://www.googleapis.com/auth/drive.file',
      callback: r => r.access_token ? (token = r.access_token, res()) : rej(new Error(r.error || 'Sign-in failed')),
    }).requestAccessToken({ prompt: '' })
  })
}
// Backend link: the browser gets a one-time code, /api/exchange (holds the client secret) turns it into a refresh token.
function codeFlow(scope, extra = {}, popup = {}) {
  return new Promise((res, rej) => {
    if (!window.google) return rej(new Error('Google script not loaded yet'))
    window.google.accounts.oauth2.initCodeClient({
      client_id: CID, scope, ux_mode: 'popup', ...popup,
      error_callback: e => rej(new Error(e?.type === 'popup_closed' ? 'Connect was cancelled' : e?.message || 'Connect failed')),
      callback: async r => {
        if (!r.code) return rej(new Error(r.error || 'Connect failed'))
        try {
          const x = await fetch('/api/exchange', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: r.code, ...extra }) })
          const j = await x.json().catch(() => ({}))
          if (!x.ok || !j.refresh_token) throw new Error(j.error || 'Google did not return a refresh token. Remove Frame AI at myaccount.google.com/permissions and connect again.')
          res(j)
        } catch (e) { rej(e) }
      },
    }).requestCode()
  })
}
export const connectBackend = () => codeFlow('https://www.googleapis.com/auth/drive.file').then(j => j.refresh_token)
// YouTube upload login for the backend. select_account lets the channel live on a different Google account than the Drive.
export const connectYouTube = () => codeFlow('https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly https://www.googleapis.com/auth/youtube.force-ssl', { profile: 'youtube' }, { select_account: true })
async function api(url, opt = {}) {
  const r = await fetch(url, { ...opt, headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', ...opt.headers } })
  if (!r.ok) throw new Error((await r.text()).slice(0, 300))
  return r.status === 204 ? null : r.json()
}
async function find(name, parent, mime) {
  const c = [`name='${name.replace(/'/g, "\\'")}'`, 'trashed=false']
  if (parent) c.push(`'${parent}' in parents`); if (mime) c.push(`mimeType='${mime}'`)
  return (await api(`${D}/files?q=${enc(c.join(' and '))}&fields=files(id,name)`)).files[0]
}
const mkFolder = (name, parent) => api(`${D}/files?fields=id`, { method: 'POST', body: JSON.stringify({ name, mimeType: FOLDER, parents: parent ? [parent] : undefined }) })
async function mkSheet(name, parent, tabs) {
  const cell = h => ({ userEnteredValue: { stringValue: h }, userEnteredFormat: { textFormat: { bold: true } } })
  const s = await api(S, { method: 'POST', body: JSON.stringify({ properties: { title: name }, sheets: tabs.map((t, i) => ({ properties: { title: t.title, sheetId: i, gridProperties: { frozenRowCount: 1 } }, data: [{ startRow: 0, startColumn: 0, rowData: [{ values: t.header.map(cell) }] }] })) }) })
  await api(`${D}/files/${s.spreadsheetId}?addParents=${parent}&removeParents=root&fields=id`, { method: 'PATCH', body: '{}' })
  return s.spreadsheetId
}
const append = (id, tab, row) => api(`${S}/${id}/values/${enc(tab + '!A1')}:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS`, { method: 'POST', body: JSON.stringify({ values: [row] }) })
const read = async (id, range) => (await api(`${S}/${id}/values/${enc(range)}`)).values || []
const log = (sheet, action, details) => append(sheet, 'Log', [now(), action, details])


const put = (id, range, rows) => api(`${S}/${id}/values/${enc(range)}?valueInputOption=USER_ENTERED`, { method: 'PUT', body: JSON.stringify({ values: rows }) })
const safe = s => /^[=+\-@]/.test(s || '') ? "'" + s : (s || '')   // stops typed text turning into a sheet formula
const ensureFolder = async (name, parent) => (await find(name, parent, FOLDER)) || mkFolder(name, parent)
const delRow = (sheet, tabId, i) => api(`${S}/${sheet}:batchUpdate`, { method: 'POST', body: JSON.stringify({ requests: [{ deleteDimension: { range: { sheetId: tabId, dimension: 'ROWS', startIndex: i + 1, endIndex: i + 2 } } }] }) })
const rowIndex = async (sheet, range, id) => (await read(sheet, range)).findIndex(r => r[0] === id)
export const fmtLen = s => s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` : ''

// Fixed, reusable things every big project (teacher / series) keeps once and every video can use.
export const GLOBAL = [
  ['Thumbnails', 'Thumbnail templates, teacher cut-out photos, title backgrounds', 'image/*,.psd,.ai'],
  ['Branding', 'Logo, watermark, lower-third (name title) graphics', 'image/*,video/*'],
  ['Intro-Outro', 'Standard intro, outro, subscribe / end-screen clips', 'video/*'],
  ['Audio', 'Ambience and sound effects (only if the teacher allows music)', 'audio/*'],
  ['Fonts', 'Urdu Nastaliq / Arabic fonts for captions and ayat', '.ttf,.otf,.woff2'],
  ['Profile', 'Teacher photo, bio, voice sample for reference', 'image/*,audio/*,.pdf,.md,.txt'],
  ['References', 'Books, PDFs, ayat / hadith source lists the teacher quotes', '.pdf,.md,.txt,.docx']]
// [label, placeholder, long text?]
export const PROFILE = [
  ['Teacher name', 'e.g. Maulana ...'], ['Honorific / title', 'e.g. حفظہ اللہ'], ['Main language', 'Urdu / Arabic / English'], ['Caption language', 'Urdu'],
  ['Brand colours', '#0b6e4f, #f5e6a8'], ['Default hashtags', '#islam #bayan'], ['Channel / social links', 'YouTube, Instagram, WhatsApp channel…'],
  ['Call to action', 'Like, share, subscribe…'], ['Default video length (s)', '60'], ['Background music allowed (yes/no)', 'no'],
  ['Content rules', 'Ayat must not be cropped, always show ﷺ, no music, …', true]]
const SCENE_HEAD = ['Video Name', 'Processed', 'Description', 'Length (s)', 'Source', 'Uploaded', 'Requirement']
const REQ_HEAD = ['Required Scene', 'Description', 'Done', 'Video Name', 'Length (s)', 'Uploaded', 'Link']
const REQ_SEED = [['Mosque exterior (b-roll)', 'Wide shot of a mosque at golden hour, 10-15 seconds, no people in focus', '', '', '', '', ''],
  ['Quran page close-up', 'Slow pan over open Quran pages, steady camera, 8-10 seconds', '', '', '', '', ''],
  ['Sky / clouds timelapse', 'Calm sky timelapse for reflective moments, 10 seconds', '', '', '', '', '']]

// ---------- workspace bootstrap ----------
// Drive layout (everything visible in one tree, nothing hidden):
//   Frame AI/
//     Application Config/  frame_config.json, Production Plan (sheet), Production Record (sheet), Projects (registry sheet), Scene/, Requirements/
//     Projects/            one folder per project (a playlist: shared narrator, thumbnail style, description)
//     Singles/             stand-alone videos, no playlist and no shared description (its own sheet, narrator and thumbnail style)
//     Social/              social_config.json (YouTube login, Buffer keys, defaults) + Days/<YYYY-MM-DD>.json, one schedule file per day
//     Brand/               brand.json + Logo/, Profile/, Banner/, Intro-Outro/ ...: ONE brand kit for every playlist and every platform
const move = (id, from, to) => api(`${D}/files/${id}?addParents=${to}&removeParents=${from}&fields=id`, { method: 'PATCH', body: '{}' })
async function ensureAppConfig(root) {
  const a = await find('Application Config', root, FOLDER)
  if (a) return a
  // older layouts kept it (or "Frame AI Config") at the top of Drive, outside Frame AI: bring it inside
  const old = (await find('Application Config', null, FOLDER)) || (await find('Frame AI Config', null, FOLDER))
  if (!old) return mkFolder('Application Config', root)
  const from = (await api(`${D}/files/${old.id}?fields=parents`)).parents?.[0]
  await api(`${D}/files/${old.id}?addParents=${root}${from ? `&removeParents=${from}` : ''}&fields=id`, { method: 'PATCH', body: JSON.stringify({ name: 'Application Config' }) })
  return old
}
// find it inside Application Config; else pull it in from the old root; else null
async function adopt(name, mime, app, root) {
  const f = await find(name, app, mime); if (f) return f
  const o = await find(name, root, mime); if (!o) return null
  await move(o.id, root, app); return o
}
// Older workspaces were shared as "anyone with the link"; that exposed frame_config.json. Remove any public permission.
async function lockDown(id) {
  try {
    const r = await api(`${D}/files/${id}/permissions?fields=permissions(id,type)`)
    for (const x of r.permissions || []) if (x.type === 'anyone') await api(`${D}/files/${id}/permissions/${x.id}`, { method: 'DELETE' })
  } catch { /* not fatal: the folder is still only reachable by its owner unless it was shared */ }
}
export async function init() {
  let root = await find('Frame AI', null, FOLDER)
  if (!root) root = await mkFolder('Frame AI')   // stays private: frame_config.json (API keys) lives inside
  await lockDown(root.id)
  const app = await ensureAppConfig(root.id)
  const ps = await adopt('Projects', SHEET, app.id, root.id)
  const projectsSheet = ps ? ps.id : await mkSheet('Projects', app.id, [{ title: 'Projects', header: [] }])
  await put(projectsSheet, 'Projects!A1:G1', [['Project Name', 'Folder ID', 'Sheet ID', 'Created', 'Videos Folder ID', 'Global Folder ID', 'Status']])
  let pf = await find('Projects', root.id, FOLDER)
  if (!pf) {   // first run of this layout: bring existing project folders into Projects/
    pf = await mkFolder('Projects', root.id)
    for (const r of await read(projectsSheet, 'Projects!A2:G')) if (r[1]) await move(r[1], root.id, pf.id).catch(() => {})
  }
  const singles = await ensureSingles(root.id)
  const social = await ensureSocial(root.id)
  const brand = await ensureBrand(root.id)
  const sc = (await adopt('Scene', FOLDER, app.id, root.id)) || await mkFolder('Scene', app.id)
  const ss = await find('Scene Videos', sc.id, SHEET)
  const sceneSheet = ss ? ss.id : await mkSheet('Scene Videos', sc.id, [{ title: 'Scenes', header: SCENE_HEAD }, { title: 'Log', header: ['Time', 'Action', 'Details'] }])
  await put(sceneSheet, 'Scenes!A1:G1', [SCENE_HEAD])
  const rq = (await adopt('Requirements', FOLDER, app.id, root.id)) || await mkFolder('Requirements', app.id)
  let rs = await find('Requirements', rq.id, SHEET), reqSheet = rs?.id
  if (!reqSheet) {
    reqSheet = await mkSheet('Requirements', rq.id, [{ title: 'Requirements', header: REQ_HEAD }, { title: 'Log', header: ['Time', 'Action', 'Details'] }])
    await put(reqSheet, 'Requirements!A2:G4', REQ_SEED)
  }
  return { rootId: root.id, appId: app.id, singles, social, brand, projectsFolderId: pf.id, projectsSheet, sceneId: sc.id, sceneSheet, reqFolderId: rq.id, reqSheet }
}

// ---------- Social: accounts, defaults and one schedule file per day. A sibling of Application Config / Projects / Singles. ----------
async function ensureSocial(root) {
  const f = (await find('Social', root, FOLDER)) || await mkFolder('Social', root)
  const days = await ensureFolder('Days', f.id)
  return { name: 'Social', folderId: f.id, daysId: days.id }
}

// ---------- Brand: the global brand kit (logo, profile picture, banner, intro/outro) shared by everything. A sibling of Social / Projects / Singles. ----------
async function ensureBrand(root) {
  const f = (await find('Brand', root, FOLDER)) || await mkFolder('Brand', root)
  return { name: 'Brand', folderId: f.id }
}

// ---------- Singles: one folder for stand-alone videos. It behaves like a playlist that has no shared description. ----------
async function ensureSingles(root) {
  const f = (await find('Singles', root, FOLDER)) || await mkFolder('Singles', root)
  const global = (await find('_Global', f.id, FOLDER)) || await mkFolder('_Global', f.id)
  for (const g of GLOBAL) await ensureFolder(g[0], global.id)
  const rec = await find('Singles - Record', f.id, SHEET)
  const sheetId = rec ? rec.id : await mkSheet('Singles - Record', f.id, [
    { title: 'Global Assets', header: ['File Name', 'Category', 'Size (MB)', 'Uploaded', 'Drive Link'] },
    { title: 'Log', header: ['Time', 'Action', 'Details'] }])
  return { name: 'Singles', isSingles: true, folderId: f.id, sheetId, globalId: global.id }
}

// ---------- big projects (teacher / series) ----------
export async function listProjects(w) {
  return (await read(w.projectsSheet, 'Projects!A2:G')).map(r => ({ name: r[0], folderId: r[1], sheetId: r[2], created: r[3], videosId: r[4], globalId: r[5], status: r[6] }))
}
export async function createProject(w, name) {
  const f = await mkFolder(name, w.projectsFolderId)
  const [videos, global] = await Promise.all([mkFolder('Videos', f.id), mkFolder('_Global', f.id)])
  await Promise.all(GLOBAL.map(g => mkFolder(g[0], global.id)))
  const sheetId = await mkSheet(`${name} - Record`, f.id, [
    { title: 'Videos', header: ['Video', 'Folder ID', 'Sheet ID', 'Created', 'Output Type', 'Length (s)', 'Status'] },
    { title: 'Global Assets', header: ['File Name', 'Category', 'Size (MB)', 'Uploaded', 'Drive Link'] },
    { title: 'Profile', header: ['Setting', 'Value'] },
    { title: 'Log', header: ['Time', 'Action', 'Details'] }])
  await append(w.projectsSheet, 'Projects', [name, f.id, sheetId, now(), videos.id, global.id, 'Created'])
  await log(sheetId, 'Project created', `Folder ${f.id}`)
  return { name, folderId: f.id, sheetId, videosId: videos.id, globalId: global.id }
}
export async function deleteProject(w, p) {
  await api(`${D}/files/${p.folderId}`, { method: 'DELETE' }) // folder + every video + global assets
  const i = await rowIndex(w.projectsSheet, 'Projects!B2:B', p.folderId)
  if (i >= 0) await delRow(w.projectsSheet, 0, i)
}

// ---------- global assets ----------
export async function uploadGlobal(p, cat, file) {
  const dir = await ensureFolder(cat, p.globalId)
  const r = await upload(file, dir.id)
  await append(p.sheetId, 'Global Assets', [file.name, cat, (file.size / 1048576).toFixed(2), now(), r.webViewLink])
  await log(p.sheetId, 'Global asset uploaded', `${cat}: ${file.name}`)
}
export async function listGlobal(p) { return (await read(p.sheetId, 'Global Assets!A2:E')).map(r => ({ name: r[0], cat: r[1], link: r[4] })) }
export async function getProfile(p) { const o = {}; (await read(p.sheetId, 'Profile!A2:B')).forEach(r => o[r[0]] = r[1]); return o }
export async function saveProfile(p, v) {
  const rows = [['Setting', 'Value'], ...PROFILE.map(([k]) => [k, safe(v[k] || '')]), ['Updated', now()]]
  await api(`${S}/${p.sheetId}/values/Profile!A1:B30:clear`, { method: 'POST', body: '{}' })
  await put(p.sheetId, 'Profile!A1', rows)
  await log(p.sheetId, 'Profile saved', 'Global defaults updated')
}

// ---------- videos (sub-projects of a big project) ----------
export async function listVideos(p) {
  return (await read(p.sheetId, 'Videos!A2:G')).map(r => ({ name: r[0], folderId: r[1], sheetId: r[2], created: r[3], mode: r[4], length: r[5], status: r[6] }))
}
export async function createVideo(p, name) {
  const f = await mkFolder(name, p.videosId)
  const sheetId = await mkSheet(`${name} - Record`, f.id, [
    { title: 'Files', header: ['File Name', 'Type', 'Size (MB)', 'Uploaded', 'Drive Link'] },
    { title: 'Settings', header: ['Setting', 'Value'] },
    { title: 'Log', header: ['Time', 'Action', 'Details'] }])
  await append(p.sheetId, 'Videos', [name, f.id, sheetId, now(), 'not set', '', 'Created'])
  await log(sheetId, 'Video created', `Folder ${f.id}`)
  await log(p.sheetId, 'Video created', name)
}
export async function deleteVideo(p, v) {
  await api(`${D}/files/${v.folderId}`, { method: 'DELETE' })
  const i = await rowIndex(p.sheetId, 'Videos!B2:B', v.folderId)
  if (i >= 0) await delRow(p.sheetId, 0, i)
  await log(p.sheetId, 'Video deleted', v.name)
}

// ---------- files inside one video (v = video record) ----------
async function upload(file, parent) {
  const r = await fetch('https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,webViewLink', { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', 'X-Upload-Content-Type': file.type || 'application/octet-stream' }, body: JSON.stringify({ name: file.name, parents: [parent] }) })
  if (!r.ok) throw new Error('Upload could not start')
  const p = await fetch(r.headers.get('Location'), { method: 'PUT', body: file })
  if (!p.ok) throw new Error('Upload failed')
  return p.json()
}
export async function uploadToVideo(v, file) {
  const r = await upload(file, v.folderId)
  await append(v.sheetId, 'Files', [file.name, file.type || file.name.split('.').pop(), (file.size / 1048576).toFixed(2), now(), r.webViewLink])
  await log(v.sheetId, 'File uploaded', `${file.name} (${(file.size / 1048576).toFixed(2)} MB)`)
}
export async function saveDescription(v, text) {
  const old = await find('description.txt', v.folderId)
  if (old) await api(`${D}/files/${old.id}`, { method: 'DELETE' })
  await upload(new File([text], 'description.txt', { type: 'text/plain' }), v.folderId)
  await log(v.sheetId, 'Description saved', `${text.length} characters`)
}
export async function getSettings(v) { const o = {}; (await read(v.sheetId, 'Settings!A2:B')).forEach(r => o[r[0]] = r[1]); return o }
export async function saveSettings(p, v, s) {
  const rows = [['Setting', 'Value'], ['Output type', s.mode], ['Video length (s)', s.length], ['Process part: start', s.start], ['Process part: end', s.end], ['Process whole video', s.whole ? 'yes' : 'no'], ['Updated', now()]]
  await api(`${S}/${v.sheetId}/values/Settings!A1:B20:clear`, { method: 'POST', body: '{}' })
  await put(v.sheetId, 'Settings!A1', rows)
  const i = await rowIndex(p.sheetId, 'Videos!B2:B', v.folderId)
  if (i >= 0) await put(p.sheetId, `Videos!E${i + 2}:G${i + 2}`, [[s.mode, s.length, 'Ready']])
  await log(v.sheetId, 'Settings updated', `${s.mode}, ${s.length}s, ${s.whole ? 'whole video' : s.start + ' to ' + s.end}`)
}

// ---------- YouTube links (recorded, not downloaded in the browser) ----------
export const ytId = u => (u.match(/(?:youtu\.be\/|v=|shorts\/|embed\/)([\w-]{11})/) || [])[1]
async function ytTitle(url) { try { return (await (await fetch(`https://noembed.com/embed?url=${enc(url)}`)).json()).title || null } catch { return null } }
async function ytInfo(url) {
  const id = ytId(url.trim()); if (!id) throw new Error('That is not a valid YouTube link')
  return { id, link: `https://www.youtube.com/watch?v=${id}`, title: (await ytTitle(url)) || `YouTube video ${id}` }
}
export async function addLinkToVideo(v, url) {
  const y = await ytInfo(url)
  await append(v.sheetId, 'Files', [y.title, 'YouTube link', '', now(), y.link])
  await log(v.sheetId, 'YouTube link added', `${y.title} (${y.link})`)
}

// ---------- scenes ----------
export async function listScenes(w) { return (await read(w.sceneSheet, 'Scenes!A2:G')).map(r => ({ name: r[0], processed: r[1], desc: r[2], len: r[3], src: r[4], when: r[5], req: r[6] })) }
const videoLength = file => new Promise(res => {
  const v = document.createElement('video'), u = URL.createObjectURL(file), done = x => { URL.revokeObjectURL(u); res(x) }
  v.preload = 'metadata'; v.onloadedmetadata = () => done(isFinite(v.duration) ? Math.round(v.duration) : null); v.onerror = () => done(null); v.src = u
})
async function sceneFile(w, file, desc, req) {
  if (!file.type.startsWith('video/')) throw new Error(`${file.name} is not a video`)
  const [len, r] = await Promise.all([videoLength(file), upload(file, w.sceneId)])
  await append(w.sceneSheet, 'Scenes', [file.name, '', safe(desc), len ?? '', 'Upload', now(), req || ''])
  await log(w.sceneSheet, 'Scene uploaded', `${file.name}${len ? ` (${fmtLen(len)})` : ''}${req ? ` for "${req}"` : ''}`)
  return { len, link: r.webViewLink }
}
async function sceneLink(w, url, desc, req) {
  const y = await ytInfo(url)
  await append(w.sceneSheet, 'Scenes', [`=HYPERLINK("${y.link}","${y.title.replace(/"/g, '""')}")`, '', safe(desc), '', 'YouTube', now(), req || ''])
  await log(w.sceneSheet, 'Scene link added', `${y.title} (${y.link})`)
  return y
}
export const uploadScene = (w, file, desc) => sceneFile(w, file, desc, '')
export const addSceneLink = (w, url, desc) => sceneLink(w, url, desc, '')

// ---------- requirements (read from the sheet, tick when fulfilled) ----------
export async function listReqs(w) {
  return (await read(w.reqSheet, 'Requirements!A2:G')).map((r, i) => ({ row: i + 2, name: r[0] || '', desc: r[1] || '', done: r[2] === '✅', video: r[3], len: r[4], link: r[6] })).filter(r => r.name)
}
export async function addReq(w, name, desc) {
  await append(w.reqSheet, 'Requirements', [safe(name), safe(desc), '', '', '', '', ''])
  await log(w.reqSheet, 'Requirement added', name)
}
const tick = (w, req, vals) => put(w.reqSheet, `Requirements!C${req.row}:G${req.row}`, [['✅', ...vals]])
export async function fulfilWithFile(w, req, file, desc) {
  const { len, link } = await sceneFile(w, file, desc, req.name)
  await tick(w, req, [file.name, len ?? '', now(), link || ''])
  await log(w.reqSheet, 'Requirement done', `${req.name} <- ${file.name}${len ? ` (${fmtLen(len)})` : ''}`)
}
export async function fulfilWithLink(w, req, url, desc) {
  const y = await sceneLink(w, url, desc, req.name)
  await tick(w, req, [y.title, '', now(), y.link])
  await log(w.reqSheet, 'Requirement done', `${req.name} <- ${y.link}`)
}

// ---------- read a script file (txt, md, srt, docx, doc, rtf, odt) as plain text ----------
// Plain text is read in the browser. Word-type files are converted by Drive to a temporary Google Doc, exported as text, then the copy is deleted.
const DOC_TYPES = { docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', doc: 'application/msword', rtf: 'application/rtf', odt: 'application/vnd.oasis.opendocument.text', pdf: 'application/pdf' }
export async function readTextFile(file) {
  const ext = file.name.split('.').pop().toLowerCase()
  if (['txt', 'md', 'srt', 'text'].includes(ext)) return (await file.text()).replace(/^\uFEFF/, '').trim()
  const type = DOC_TYPES[ext]
  if (!type) throw new Error('Upload a .txt, .md, .pdf, .docx, .doc, .rtf or .odt script file')
  const r = await fetch('https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id', { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', 'X-Upload-Content-Type': type }, body: JSON.stringify({ name: `import ${file.name}`, mimeType: 'application/vnd.google-apps.document' }) })
  if (!r.ok) throw new Error('Could not start reading the script')
  const up = await fetch(r.headers.get('Location'), { method: 'PUT', body: file })
  if (!up.ok) throw new Error('Could not read that script file')
  const id = (await up.json()).id
  try {
    const t = await fetch(`${D}/files/${id}/export?mimeType=text/plain`, { headers: { Authorization: `Bearer ${token}` } })
    if (!t.ok) throw new Error('Could not read that script file')
    return (await t.text()).replace(/^\uFEFF/, '').trim()
  } finally { api(`${D}/files/${id}`, { method: 'DELETE' }).catch(() => {}) }
}

// ---------- read an uploaded spreadsheet file (xlsx, xls, csv, ods) ----------
// Drive converts the file to a temporary Google Sheet, we read the cells, then delete the temporary copy.
const SHEET_TYPES = { csv: 'text/csv', tsv: 'text/tab-separated-values', xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', xls: 'application/vnd.ms-excel', ods: 'application/vnd.oasis.opendocument.spreadsheet' }
export async function readTable(file) {
  const type = SHEET_TYPES[file.name.split('.').pop().toLowerCase()]
  if (!type) throw new Error('Upload an Excel (.xlsx / .xls), CSV or ODS file')
  const r = await fetch('https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id', { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', 'X-Upload-Content-Type': type }, body: JSON.stringify({ name: `import ${file.name}`, mimeType: SHEET }) })
  if (!r.ok) throw new Error('Could not start reading the file')
  const up = await fetch(r.headers.get('Location'), { method: 'PUT', body: file })
  if (!up.ok) throw new Error('Could not read that file')
  const id = (await up.json()).id
  try {
    const title = (await api(`${S}/${id}?fields=sheets.properties.title`)).sheets[0].properties.title
    return await read(id, `'${title.replace(/'/g, "''")}'`)
  } finally { api(`${D}/files/${id}`, { method: 'DELETE' }).catch(() => {}) }
}

// internals shared with playlist.js (config files, playlist sheet, thumbnails)
export { api, find, move, mkSheet, mkFolder, ensureFolder, put, read, append, log, safe, now, upload, delRow, enc, D, S }
