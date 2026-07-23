use aes::Aes256;
use cbc::cipher::{block_padding::NoPadding, BlockDecryptMut, KeyIvInit};
use chrono::{DateTime, Local, NaiveDate, NaiveDateTime, TimeZone, Utc};
use reqwest::blocking::Client;
use rusqlite::{params, Connection, Row};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::net::ToSocketAddrs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tiny_http::{Header, Method, Request, Response, Server, StatusCode};

type Aes256CbcDec = cbc::Decryptor<Aes256>;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const DEFAULT_BRIDGE_BASE_URL: &str = "http://127.0.0.1:18081";
const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 18082;
const PAGE_SZ: usize = 4096;
const SALT_SZ: usize = 16;
const IV_SZ: usize = 16;
const RESERVE_SZ: usize = 80;
const SQLITE_HDR: &[u8; 16] = b"SQLite format 3\0";
const WAL_HEADER_SZ: usize = 32;
const WAL_FRAME_HEADER_SZ: usize = 24;

#[derive(Clone, Debug, Serialize, Deserialize)]
struct CollectorConfig {
    #[serde(default = "default_bridge_base_url")]
    bridge_base_url: String,
    #[serde(default = "default_service_name")]
    service_name: String,
    #[serde(default = "default_event_name")]
    event_name: String,
    #[serde(default = "default_poll_interval")]
    poll_interval_secs: f64,
    #[serde(default = "default_batch_size")]
    batch_size: usize,
    #[serde(default = "default_state_dir_string")]
    state_dir: String,
    #[serde(default = "default_method_host")]
    method_host: String,
    #[serde(default = "default_method_port")]
    method_port: u16,
    #[serde(default)]
    bridge_event_token: Option<String>,
    #[serde(default)]
    service_registration_token: Option<String>,
    #[serde(default)]
    wechat_decrypt_dir: Option<String>,
    #[serde(default)]
    wechat_decrypt_config: Option<String>,
    #[serde(default)]
    db_dir: Option<String>,
    #[serde(default)]
    keys_file: Option<String>,
    #[serde(default)]
    decrypted_dir: Option<String>,
    #[serde(default = "default_true")]
    include_text: bool,
    #[serde(default = "default_true")]
    include_outgoing: bool,
}

impl Default for CollectorConfig {
    fn default() -> Self {
        Self {
            bridge_base_url: default_bridge_base_url(),
            service_name: default_service_name(),
            event_name: default_event_name(),
            poll_interval_secs: default_poll_interval(),
            batch_size: default_batch_size(),
            state_dir: default_state_dir_string(),
            method_host: default_method_host(),
            method_port: default_method_port(),
            bridge_event_token: None,
            service_registration_token: None,
            wechat_decrypt_dir: None,
            wechat_decrypt_config: None,
            db_dir: None,
            keys_file: None,
            decrypted_dir: None,
            include_text: true,
            include_outgoing: true,
        }
    }
}

impl CollectorConfig {
    fn load(path: Option<&str>) -> Result<Self, String> {
        let path = path.map(expand_home).unwrap_or_else(|| default_state_dir().join("config.json"));
        let mut cfg = if path.exists() {
            serde_json::from_str(&fs::read_to_string(&path).map_err(|e| e.to_string())?)
                .map_err(|e| e.to_string())?
        } else {
            Self::default()
        };

        if let Ok(value) = env::var("BRIDGE_AGENT_EVENT_TOKEN") {
            if !value.is_empty() {
                cfg.bridge_event_token = Some(value);
            }
        }
        if let Ok(value) = env::var("BRIDGE_AGENT_SERVICE_REGISTRATION_TOKEN") {
            if !value.is_empty() {
                cfg.service_registration_token = Some(value);
            }
        }
        if is_loopback_url(&cfg.bridge_base_url) {
            let tokens = load_bridge_agent_tokens();
            if cfg.bridge_event_token.as_deref().unwrap_or("").is_empty() {
                cfg.bridge_event_token = tokens.get("event_server_token").cloned();
            }
            if cfg.service_registration_token.as_deref().unwrap_or("").is_empty() {
                cfg.service_registration_token = tokens.get("service_registration_token").cloned();
            }
        }
        if let Ok(value) = env::var("WECHAT_DECRYPT_DIR") {
            if !value.is_empty() {
                cfg.wechat_decrypt_dir = Some(value);
            }
        }
        Ok(cfg)
    }

    fn save(&self, path: Option<PathBuf>) -> Result<PathBuf, String> {
        let path = path.unwrap_or_else(|| self.config_path());
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let text = serde_json::to_string_pretty(self).map_err(|e| e.to_string())? + "\n";
        fs::write(&path, text).map_err(|e| e.to_string())?;
        Ok(path)
    }

    fn state_dir_path(&self) -> PathBuf {
        expand_home(&self.state_dir)
    }

    fn state_path(&self) -> PathBuf {
        self.state_dir_path().join("state.json")
    }

    fn config_path(&self) -> PathBuf {
        self.state_dir_path().join("config.json")
    }

    fn default_keys_path(&self) -> PathBuf {
        self.state_dir_path().join("all_keys.json")
    }

    fn default_decrypted_path(&self) -> PathBuf {
        self.state_dir_path().join("decrypted")
    }

    fn method_base_url(&self) -> String {
        format!("http://{}:{}", self.method_host, self.method_port)
    }

    fn bridge_events_url(&self) -> String {
        format!("{}/v1/events", self.bridge_base_url.trim_end_matches('/'))
    }

    fn bridge_services_url(&self) -> String {
        format!("{}/v1/services", self.bridge_base_url.trim_end_matches('/'))
    }

    fn resolved_wechat_decrypt_dir(&self) -> Result<PathBuf, String> {
        let mut candidates = Vec::new();
        if let Some(value) = &self.wechat_decrypt_dir {
            candidates.push(expand_home(value));
        }
        if let Ok(cwd) = env::current_dir() {
            candidates.push(cwd.join("vendor/wechat-decrypt"));
            if let Some(parent) = cwd.parent() {
                candidates.push(parent.join("wechat-decrypt"));
            }
        }
        candidates.push(home_dir().join("dev/wechat-decrypt"));
        for path in candidates {
            if path.join("key_utils.py").is_file() || path.join("find_all_keys_macos.c").is_file() {
                return Ok(path);
            }
        }
        Err("wechat-decrypt source directory was not found. Set WECHAT_DECRYPT_DIR or collector config `wechat_decrypt_dir`.".to_string())
    }

    fn load_wechat_runtime(&self) -> Result<WechatRuntime, String> {
        let wd_dir = self.resolved_wechat_decrypt_dir()?;
        let raw = if let Some(path) = &self.wechat_decrypt_config {
            let path = expand_home(path);
            if path.exists() {
                serde_json::from_str::<Value>(&fs::read_to_string(path).map_err(|e| e.to_string())?).unwrap_or_else(|_| json!({}))
            } else {
                json!({})
            }
        } else {
            json!({})
        };
        let db_dir = self
            .db_dir
            .clone()
            .or_else(|| raw.get("db_dir").and_then(Value::as_str).map(str::to_string))
            .or_else(auto_detect_db_dir)
            .ok_or_else(|| "WeChat db_storage directory was not configured. Run setup, or set collector `db_dir`.".to_string())?;

        Ok(WechatRuntime {
            wechat_decrypt_dir: wd_dir,
            db_dir: expand_home(&db_dir),
            keys_file: resolve_state_path(
                self.keys_file.as_deref().or_else(|| raw.get("keys_file").and_then(Value::as_str)),
                &self.state_dir_path(),
                self.default_keys_path(),
            ),
            _decrypted_dir: resolve_state_path(
                self.decrypted_dir.as_deref().or_else(|| raw.get("decrypted_dir").and_then(Value::as_str)),
                &self.state_dir_path(),
                self.default_decrypted_path(),
            ),
        })
    }
}

#[derive(Clone, Debug)]
struct WechatRuntime {
    wechat_decrypt_dir: PathBuf,
    db_dir: PathBuf,
    keys_file: PathBuf,
    _decrypted_dir: PathBuf,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
struct Cursor {
    #[serde(default)]
    create_time: i64,
    #[serde(default)]
    local_id: i64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct CollectorState {
    #[serde(default = "default_schema_version")]
    schema_version: i64,
    #[serde(default)]
    sessions: BTreeMap<String, i64>,
    #[serde(default)]
    cursors: BTreeMap<String, Cursor>,
}

impl Default for CollectorState {
    fn default() -> Self {
        Self {
            schema_version: 1,
            sessions: BTreeMap::new(),
            cursors: BTreeMap::new(),
        }
    }
}

impl CollectorState {
    fn load(path: &Path) -> Self {
        fs::read_to_string(path)
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_default()
    }

    fn save(&self, path: &Path) -> Result<(), String> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let tmp = path.with_extension(format!("{}tmp", path.extension().and_then(|v| v.to_str()).unwrap_or("json.")));
        fs::write(&tmp, serde_json::to_string_pretty(self).map_err(|e| e.to_string())? + "\n").map_err(|e| e.to_string())?;
        fs::rename(tmp, path).map_err(|e| e.to_string())
    }

    fn cursor_for(&self, key: &str) -> Cursor {
        self.cursors.get(key).cloned().unwrap_or_default()
    }

    fn set_cursor(&mut self, key: String, create_time: i64, local_id: i64) {
        if let Some(current) = self.cursors.get(&key) {
            if (create_time, local_id) < (current.create_time, current.local_id) {
                return;
            }
        }
        self.cursors.insert(key, Cursor { create_time, local_id });
    }
}

#[derive(Clone)]
struct MessageCandidate {
    event_id: String,
    payload: Value,
    occurred_at: String,
    cursor_key: String,
    cursor: Cursor,
}

#[derive(Clone)]
struct DBCache {
    keys: Arc<HashMap<String, Value>>,
    db_dir: PathBuf,
    cache_dir: PathBuf,
    cache: Arc<Mutex<HashMap<String, (FileSig4, PathBuf)>>>,
    metadata_path: PathBuf,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
struct FileSig4(f64, u64, f64, u64);

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
struct FileSig2(f64, u64);

impl DBCache {
    fn new(keys: HashMap<String, Value>, db_dir: PathBuf) -> Self {
        let cache_dir = env::temp_dir().join("wechat_bridge_collector_cache");
        let _ = fs::create_dir_all(&cache_dir);
        let metadata_path = cache_dir.join("_snapshots.json");
        let this = Self {
            keys: Arc::new(keys),
            db_dir,
            cache_dir,
            cache: Arc::new(Mutex::new(HashMap::new())),
            metadata_path,
        };
        this.load_persistent_cache();
        this
    }

    fn get(&self, rel_key: &str) -> Result<Option<PathBuf>, String> {
        let Some(key_info) = self.key_info(rel_key) else {
            return Ok(None);
        };
        let db_path = self.db_dir.join(rel_key.replace('\\', "/"));
        let wal_path = PathBuf::from(format!("{}-wal", db_path.display()));
        if !db_path.exists() {
            return Ok(None);
        }
        let Some(signature) = file_sig4(&db_path, &wal_path) else {
            return Ok(None);
        };
        if let Some((cached_sig, path)) = self.cache.lock().map_err(|e| e.to_string())?.get(rel_key).cloned() {
            if cached_sig == signature && path.exists() {
                return Ok(Some(path));
            }
        }

        let out_path = self.cache_dir.join(format!("{:x}.db", md5::compute(format!("{}:{rel_key}", self.db_dir.display()))));
        if key_info.get("plain").and_then(Value::as_bool) == Some(true) {
            fs::copy(&db_path, &out_path).map_err(|e| e.to_string())?;
            assert_sqlite_healthy(&out_path)?;
            self.cache.lock().map_err(|e| e.to_string())?.insert(rel_key.to_string(), (signature, out_path.clone()));
            self.save_persistent_cache();
            return Ok(Some(out_path));
        }

        let enc_hex = key_info
            .get("enc_key")
            .and_then(Value::as_str)
            .ok_or_else(|| format!("missing enc_key for {rel_key}"))?;
        let enc_key = hex::decode(enc_hex).map_err(|e| e.to_string())?;
        let mut last_error = String::new();
        for attempt in 0..3 {
            let Some(current_sig) = file_sig4(&db_path, &wal_path) else {
                return Ok(None);
            };
            let tmp_path = self.cache_dir.join(format!(".{:x}.{}.{}.tmp", md5::compute(rel_key), std::process::id(), attempt));
            match full_decrypt(&db_path, &tmp_path, &enc_key)
                .and_then(|_| if wal_path.exists() { decrypt_wal(&wal_path, &tmp_path, &enc_key) } else { Ok(()) })
                .and_then(|_| {
                    if file_sig4(&db_path, &wal_path) != Some(current_sig) {
                        Err("source database changed while building decrypted snapshot".to_string())
                    } else {
                        assert_sqlite_healthy(&tmp_path)
                    }
                }) {
                Ok(()) => {
                    fs::rename(&tmp_path, &out_path).map_err(|e| e.to_string())?;
                    self.cache.lock().map_err(|e| e.to_string())?.insert(rel_key.to_string(), (current_sig, out_path.clone()));
                    self.save_persistent_cache();
                    return Ok(Some(out_path));
                }
                Err(error) => {
                    last_error = error;
                    let _ = fs::remove_file(&tmp_path);
                    thread::sleep(Duration::from_millis(50));
                }
            }
        }
        Err(format!("failed to build healthy SQLite snapshot for {rel_key}: {last_error}"))
    }

    fn key_info(&self, rel_key: &str) -> Option<Value> {
        let normalized = rel_key.replace('\\', "/");
        let variants = [
            rel_key.to_string(),
            normalized.clone(),
            normalized.replace('/', "\\"),
            normalized.replace('/', std::path::MAIN_SEPARATOR_STR),
        ];
        variants.into_iter().find_map(|candidate| {
            self.keys
                .get(&candidate)
                .filter(|value| value.get("enc_key").is_some())
                .cloned()
        })
    }

    fn load_persistent_cache(&self) {
        let Ok(text) = fs::read_to_string(&self.metadata_path) else {
            return;
        };
        let Ok(raw) = serde_json::from_str::<Value>(&text) else {
            return;
        };
        let Some(obj) = raw.as_object() else {
            return;
        };
        let mut cache = match self.cache.lock() {
            Ok(cache) => cache,
            Err(_) => return,
        };
        for (rel_key, item) in obj {
            let Some(path) = item.get("path").and_then(Value::as_str).map(PathBuf::from) else {
                continue;
            };
            if !path.exists() {
                continue;
            }
            let Some(sig) = item.get("signature").and_then(Value::as_array) else {
                continue;
            };
            if sig.len() != 4 {
                continue;
            }
            let Some(db_mtime) = sig[0].as_f64() else { continue; };
            let Some(db_size) = sig[1].as_u64() else { continue; };
            let Some(wal_mtime) = sig[2].as_f64() else { continue; };
            let Some(wal_size) = sig[3].as_u64() else { continue; };
            cache.insert(rel_key.clone(), (FileSig4(db_mtime, db_size, wal_mtime, wal_size), path));
        }
    }

    fn save_persistent_cache(&self) {
        let cache = match self.cache.lock() {
            Ok(cache) => cache,
            Err(_) => return,
        };
        let mut data = Map::new();
        for (rel_key, (FileSig4(a, b, c, d), path)) in cache.iter() {
            data.insert(rel_key.clone(), json!({"signature": [a, b, c, d], "path": path}));
        }
        let tmp = self.metadata_path.with_extension("tmp");
        if fs::write(&tmp, Value::Object(data).to_string()).is_ok() {
            let _ = fs::rename(tmp, &self.metadata_path);
        }
    }
}

struct WeChatSource {
    config: CollectorConfig,
    runtime: WechatRuntime,
    all_keys: HashMap<String, Value>,
    db_dir: PathBuf,
    cache: DBCache,
    msg_db_keys: Vec<String>,
    contacts_cache: Mutex<Option<(Option<FileSig2>, Vec<Value>)>>,
    session_cache: Mutex<Option<(Option<FileSig2>, BTreeMap<String, i64>)>>,
}

impl WeChatSource {
    fn new(config: CollectorConfig) -> Result<Self, String> {
        let runtime = config.load_wechat_runtime()?;
        if !runtime.keys_file.exists() {
            return Err(format!("wechat-decrypt keys file does not exist: {}. Run setup first.", runtime.keys_file.display()));
        }
        let raw: Value = serde_json::from_str(&fs::read_to_string(&runtime.keys_file).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
        let mut all_keys = HashMap::new();
        if let Some(obj) = raw.as_object() {
            for (key, value) in obj {
                if !key.starts_with('_') {
                    all_keys.insert(key.clone(), value.clone());
                }
            }
        }
        let msg_db_keys = find_msg_db_keys(&all_keys);
        let cache = DBCache::new(all_keys.clone(), runtime.db_dir.clone());
        Ok(Self {
            db_dir: runtime.db_dir.clone(),
            config,
            runtime,
            all_keys,
            cache,
            msg_db_keys,
            contacts_cache: Mutex::new(None),
            session_cache: Mutex::new(None),
        })
    }

    fn probe(&self) -> Value {
        let names = self.contact_names();
        let sessions = self.read_session_state();
        let mut msg_tables = 0_i64;
        for rel_key in &self.msg_db_keys {
            if let Ok(Some(path)) = self.cache.get(rel_key) {
                if let Ok(conn) = Connection::open(path) {
                    msg_tables += conn
                        .query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'", [], |row| row.get::<_, i64>(0))
                        .unwrap_or(0);
                }
            }
        }
        json!({
            "wechat_decrypt_dir": self.runtime.wechat_decrypt_dir,
            "db_dir": self.db_dir,
            "keys_file": self.runtime.keys_file,
            "key_count": self.all_keys.len(),
            "message_db_count": self.msg_db_keys.len(),
            "message_table_count": msg_tables,
            "session_count": sessions.len(),
            "contact_name_count": names.len(),
        })
    }

    fn contact_names(&self) -> BTreeMap<String, String> {
        self.all_contacts()
            .0
            .into_iter()
            .filter_map(|item| {
                let username = item.get("username")?.as_str()?.to_string();
                let display = item.get("displayName")?.as_str()?.to_string();
                Some((username, display))
            })
            .collect()
    }

    fn contacts(&self, query: &str, limit: usize) -> Vec<Value> {
        let query = query.trim().to_lowercase();
        let mut contacts = self
            .all_contacts()
            .0
            .into_iter()
            .filter(|item| {
                query.is_empty()
                    || ["username", "displayName", "nickName", "remark"].iter().any(|key| {
                        item.get(*key).and_then(Value::as_str).unwrap_or("").to_lowercase().contains(&query)
                    })
            })
            .collect::<Vec<_>>();
        contacts.sort_by(|a, b| {
            let ar = a.get("remark").and_then(Value::as_str).unwrap_or("").is_empty();
            let br = b.get("remark").and_then(Value::as_str).unwrap_or("").is_empty();
            ar.cmp(&br).then_with(|| a.get("displayName").and_then(Value::as_str).unwrap_or("").to_lowercase().cmp(&b.get("displayName").and_then(Value::as_str).unwrap_or("").to_lowercase()))
        });
        contacts.truncate(normalize_limit(limit, 100_000));
        contacts
    }

    fn all_contacts(&self) -> (Vec<Value>, Option<FileSig2>) {
        let path = match self.cache.get("contact/contact.db") {
            Ok(Some(path)) => path,
            _ => return (Vec::new(), None),
        };
        let signature = file_sig2(&path);
        if let Ok(cache) = self.contacts_cache.lock() {
            if let Some((cached_sig, items)) = cache.as_ref() {
                if *cached_sig == signature {
                    return (items.clone(), signature);
                }
            }
        }
        let mut contacts = Vec::new();
        if let Ok(conn) = Connection::open(&path) {
            if let Ok(mut stmt) = conn.prepare("SELECT username, nick_name, remark FROM contact") {
                if let Ok(rows) = stmt.query_map([], |row| {
                    let username: String = row.get(0)?;
                    let nick: Option<String> = row.get(1)?;
                    let remark: Option<String> = row.get(2)?;
                    Ok((username, nick.unwrap_or_default(), remark.unwrap_or_default()))
                }) {
                    for row in rows.flatten() {
                        if row.0.is_empty() {
                            continue;
                        }
                        let display = if !row.2.is_empty() { row.2.clone() } else if !row.1.is_empty() { row.1.clone() } else { row.0.clone() };
                        contacts.push(json!({
                            "username": row.0,
                            "displayName": display,
                            "nickName": row.1,
                            "remark": row.2,
                            "isGroup": row.0.contains("@chatroom"),
                        }));
                    }
                }
            }
        }
        if let Ok(mut cache) = self.contacts_cache.lock() {
            *cache = Some((signature, contacts.clone()));
        }
        (contacts, signature)
    }

    fn read_session_state(&self) -> BTreeMap<String, i64> {
        let path = match self.cache.get("session/session.db") {
            Ok(Some(path)) => path,
            _ => return BTreeMap::new(),
        };
        let signature = file_sig2(&path);
        if let Ok(cache) = self.session_cache.lock() {
            if let Some((cached_sig, items)) = cache.as_ref() {
                if *cached_sig == signature {
                    return items.clone();
                }
            }
        }
        let mut state = BTreeMap::new();
        if let Ok(conn) = Connection::open(path) {
            if let Ok(mut stmt) = conn.prepare("SELECT username, last_timestamp FROM SessionTable WHERE last_timestamp > 0") {
                if let Ok(rows) = stmt.query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))) {
                    for row in rows.flatten() {
                        if !row.0.is_empty() {
                            state.insert(row.0, row.1);
                        }
                    }
                }
            }
        }
        if let Ok(mut cache) = self.session_cache.lock() {
            *cache = Some((signature, state.clone()));
        }
        state
    }

    fn recent_sessions(&self, limit: usize) -> Vec<Value> {
        let path = match self.cache.get("session/session.db") {
            Ok(Some(path)) => path,
            _ => return Vec::new(),
        };
        let names = self.contact_names();
        let mut sessions = Vec::new();
        if let Ok(conn) = Connection::open(path) {
            let sql = "SELECT username, unread_count, summary, last_timestamp, last_msg_type, last_msg_sender, last_sender_display_name FROM SessionTable WHERE last_timestamp > 0 ORDER BY last_timestamp DESC LIMIT ?";
            let fallback = "SELECT username, 0, '', last_timestamp, 0, '', '' FROM SessionTable WHERE last_timestamp > 0 ORDER BY last_timestamp DESC LIMIT ?";
            let rows = query_session_rows(&conn, sql, normalize_limit(limit, 200)).or_else(|_| query_session_rows(&conn, fallback, normalize_limit(limit, 200))).unwrap_or_default();
            for row in rows {
                let is_group = row.username.contains("@chatroom");
                let summary = decompress_content(row.summary, None).unwrap_or_default();
                let (sender_from_content, text) = parse_message_content(&summary, is_group);
                let sender_id = if !row.last_msg_sender.is_empty() { row.last_msg_sender } else { sender_from_content };
                let (type_name, _) = type_label(row.last_msg_type & 0xFFFF_FFFF);
                sessions.push(json!({
                    "conversationId": row.username,
                    "conversationName": names.get(&row.username).cloned().unwrap_or(row.username.clone()),
                    "isGroup": is_group,
                    "unreadCount": row.unread_count,
                    "summary": text,
                    "lastTimestamp": row.last_timestamp,
                    "lastOccurredAt": timestamp_to_iso(row.last_timestamp),
                    "lastMessageType": type_name,
                    "lastSenderId": sender_id,
                    "lastSenderName": names.get(&sender_id).cloned().unwrap_or(if !row.last_sender_display_name.is_empty() { row.last_sender_display_name } else { sender_id.clone() }),
                }));
            }
        }
        sessions
    }

    fn bootstrap_state(&self, state: &mut CollectorState, backfill_seconds: i64) {
        state.sessions = self.read_session_state();
        let fixed = if backfill_seconds > 0 {
            Some(Cursor { create_time: Utc::now().timestamp() - backfill_seconds, local_id: 0 })
        } else {
            None
        };
        for rel_key in &self.msg_db_keys {
            let Ok(Some(path)) = self.cache.get(rel_key) else { continue; };
            let Ok(conn) = Connection::open(path) else { continue; };
            let Ok(mut stmt) = conn.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'") else { continue; };
            let Ok(rows) = stmt.query_map([], |row| row.get::<_, String>(0)) else { continue; };
            for table in rows.flatten().filter(|name| is_msg_table(name)) {
                let cursor = fixed.clone().unwrap_or_else(|| max_cursor(&conn, &table));
                state.set_cursor(cursor_key(rel_key, &table), cursor.create_time, cursor.local_id);
            }
        }
    }

    fn changed_usernames(&self, state: &CollectorState) -> (BTreeMap<String, i64>, Vec<String>) {
        let current = self.read_session_state();
        let changed = current
            .iter()
            .filter(|(username, ts)| **ts > *state.sessions.get(*username).unwrap_or(&0))
            .map(|(username, _)| username.clone())
            .collect();
        (current, changed)
    }

    fn iter_new_messages(&self, state: &CollectorState, usernames: &[String], batch_size: usize) -> Vec<MessageCandidate> {
        let names = self.contact_names();
        let mut output = Vec::new();
        for username in usernames {
            for (rel_key, table_name, path) in self.message_tables_for_username(username) {
                let cursor = state.cursor_for(&cursor_key(&rel_key, &table_name));
                output.extend(self.query_table(&path, &rel_key, &table_name, username, &names, &cursor, batch_size));
            }
        }
        output
    }

    fn get_chat_history(&self, conversation_id: &str, limit: usize, offset: usize, start_time: &Value, end_time: &Value, oldest_first: bool, message_types: Option<&Value>) -> Result<Value, String> {
        let ctx = self.conversation_context(conversation_id)?;
        let cid = ctx["conversationId"].as_str().unwrap_or("");
        self.require_message_tables(cid)?;
        let type_filter = resolve_type_filter(message_types)?;
        let (start_ts, end_ts) = parse_time_range(start_time, end_time)?;
        let messages = self.query_messages_for_username(cid, limit, offset, start_ts, end_ts, oldest_first, "", type_filter);
        Ok(json!({
            "conversation": ctx,
            "messages": messages,
            "limit": normalize_limit(limit, 500),
            "offset": offset,
            "hasMoreHint": messages.len() >= normalize_limit(limit, 500),
        }))
    }

    fn search_messages(&self, keyword: &str, conversation_id: &str, limit: usize, offset: usize, start_time: &Value, end_time: &Value) -> Result<Value, String> {
        let keyword = keyword.trim();
        if keyword.is_empty() {
            return Err("keyword 不能为空".to_string());
        }
        let (start_ts, end_ts) = parse_time_range(start_time, end_time)?;
        let (ctx, usernames) = if conversation_id.trim().is_empty() {
            (Value::Null, self.known_conversation_ids())
        } else {
            let ctx = self.conversation_context(conversation_id)?;
            let cid = ctx["conversationId"].as_str().unwrap_or("").to_string();
            self.require_message_tables(&cid)?;
            (ctx, vec![cid])
        };
        let mut all = Vec::new();
        for username in usernames {
            all.extend(self.query_messages_for_username(&username, normalize_limit(limit, 500) + offset, 0, start_ts, end_ts, false, keyword, None));
        }
        all.sort_by(|a, b| message_sort_key(b).cmp(&message_sort_key(a)));
        let page = all.into_iter().skip(offset).take(normalize_limit(limit, 500)).collect::<Vec<_>>();
        Ok(json!({"conversation": ctx, "keyword": keyword, "messages": page, "limit": normalize_limit(limit, 500), "offset": offset, "hasMoreHint": page.len() >= normalize_limit(limit, 500)}))
    }

    fn get_message_by_id(&self, message_id: &str) -> Result<Value, String> {
        let (rel_key, table_name, local_id) = parse_message_id(message_id)?;
        let Some(path) = self.cache.get(&rel_key)? else {
            return Ok(Value::Null);
        };
        let names = self.contact_names();
        let username = self.username_for_table(&table_name).unwrap_or_default();
        let conn = Connection::open(path).map_err(|e| e.to_string())?;
        let id_to_username = load_name2id_maps(&conn);
        let has_ct = has_column(&conn, &table_name, "WCDB_CT_message_content");
        let sql = format!("SELECT local_id, local_type, create_time, real_sender_id, message_content, {} FROM [{}] WHERE local_id = ? LIMIT 1", if has_ct { "WCDB_CT_message_content" } else { "NULL" }, table_name);
        let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
        let row = stmt.query_row(params![local_id], message_row_from_row).ok();
        let Some(row) = row else {
            return Ok(Value::Null);
        };
        let username = if username.is_empty() { self.username_for_message_row(&row, &names).unwrap_or_default() } else { username };
        Ok(self.build_candidate(row, &rel_key, &table_name, &username, &names, &id_to_username).map(|c| c.payload).unwrap_or(Value::Null))
    }

    fn conversation_context(&self, conversation_id: &str) -> Result<Value, String> {
        let conversation_id = conversation_id.trim();
        if conversation_id.is_empty() {
            return Err("conversationId 不能为空".to_string());
        }
        let names = self.contact_names();
        Ok(json!({
            "conversationId": conversation_id,
            "conversationName": names.get(conversation_id).cloned().unwrap_or_else(|| conversation_id.to_string()),
            "isGroup": conversation_id.contains("@chatroom"),
        }))
    }

    fn require_message_tables(&self, conversation_id: &str) -> Result<(), String> {
        if self.message_tables_for_username(conversation_id).is_empty() {
            Err(format!("找不到会话消息表: {conversation_id}"))
        } else {
            Ok(())
        }
    }

    fn known_conversation_ids(&self) -> Vec<String> {
        let mut set = self.read_session_state().keys().cloned().collect::<HashSet<_>>();
        for contact in self.all_contacts().0 {
            if let Some(username) = contact.get("username").and_then(Value::as_str) {
                set.insert(username.to_string());
            }
        }
        let mut items = set.into_iter().filter(|v| !v.is_empty()).collect::<Vec<_>>();
        items.sort();
        items
    }

    fn message_tables_for_username(&self, username: &str) -> Vec<(String, String, PathBuf)> {
        let table_name = format!("Msg_{:x}", md5::compute(username.as_bytes()));
        let mut matches = Vec::new();
        for rel_key in &self.msg_db_keys {
            let Ok(Some(path)) = self.cache.get(rel_key) else { continue; };
            let Ok(conn) = Connection::open(&path) else { continue; };
            let exists = conn
                .query_row("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", params![table_name], |_| Ok(()))
                .is_ok();
            if exists {
                matches.push((rel_key.clone(), table_name.clone(), path));
            }
        }
        matches
    }

    fn query_messages_for_username(&self, username: &str, limit: usize, offset: usize, start_ts: Option<i64>, end_ts: Option<i64>, oldest_first: bool, keyword: &str, type_filter: Option<HashSet<i64>>) -> Vec<Value> {
        let names = self.contact_names();
        let mut collected = Vec::new();
        let candidate_limit = normalize_limit(limit, 500) + offset;
        for (rel_key, table_name, path) in self.message_tables_for_username(username) {
            let Ok(conn) = Connection::open(path) else { continue; };
            let id_to_username = load_name2id_maps(&conn);
            for row in query_table_rows(&conn, &table_name, start_ts, end_ts, type_filter.as_ref(), candidate_limit, oldest_first) {
                let Some(candidate) = self.build_candidate(row, &rel_key, &table_name, username, &names, &id_to_username) else { continue; };
                let text = candidate.payload.get("text").and_then(Value::as_str).unwrap_or("");
                if keyword.is_empty() || text.to_lowercase().contains(&keyword.to_lowercase()) {
                    collected.push(candidate.payload);
                }
            }
        }
        if oldest_first {
            collected.sort_by_key(message_sort_key);
        } else {
            collected.sort_by(|a, b| message_sort_key(b).cmp(&message_sort_key(a)));
        }
        collected.into_iter().skip(offset).take(normalize_limit(limit, 500)).collect()
    }

    fn query_table(&self, db_path: &Path, rel_key: &str, table_name: &str, username: &str, names: &BTreeMap<String, String>, cursor: &Cursor, batch_size: usize) -> Vec<MessageCandidate> {
        let Ok(conn) = Connection::open(db_path) else {
            return Vec::new();
        };
        let id_to_username = load_name2id_maps(&conn);
        let has_ct = has_column(&conn, table_name, "WCDB_CT_message_content");
        let sql = format!(
            "SELECT local_id, local_type, create_time, real_sender_id, message_content, {} FROM [{}] WHERE create_time > ? OR (create_time = ? AND local_id > ?) ORDER BY create_time ASC, local_id ASC LIMIT ?",
            if has_ct { "WCDB_CT_message_content" } else { "NULL" },
            table_name
        );
        let mut output = Vec::new();
        if let Ok(mut stmt) = conn.prepare(&sql) {
            if let Ok(rows) = stmt.query_map(params![cursor.create_time, cursor.create_time, cursor.local_id, batch_size as i64], message_row_from_row) {
                for row in rows.flatten() {
                    if let Some(candidate) = self.build_candidate(row, rel_key, table_name, username, names, &id_to_username) {
                        output.push(candidate);
                    }
                }
            }
        }
        output
    }

    fn build_candidate(&self, row: MessageRow, rel_key: &str, table_name: &str, username: &str, names: &BTreeMap<String, String>, id_to_username: &HashMap<i64, String>) -> Option<MessageCandidate> {
        let content = decompress_content(row.message_content, row.ct).unwrap_or_default();
        let is_group = username.contains("@chatroom");
        let (sender_from_content, text) = parse_message_content(&content, is_group);
        let sender_username = id_to_username.get(&row.real_sender_id).cloned().unwrap_or_default();
        let sender_username = if sender_username.is_empty() { sender_from_content } else { sender_username };
        let (type_name, type_label) = type_label(row.local_type & 0xFFFF_FFFF);
        let direction = direction_for(is_group, username, &sender_username);
        if direction == "outgoing" && !self.config.include_outgoing {
            return None;
        }
        let message_id = format!("{rel_key}:{table_name}:{}", row.local_id);
        let event_id = format!("{:x}", Sha256::digest(message_id.as_bytes()));
        let occurred_at = Utc.timestamp_opt(row.create_time, 0).single().unwrap_or_else(Utc::now).to_rfc3339();
        let mut payload = json!({
            "messageId": message_id,
            "dbPath": rel_key,
            "tableName": table_name,
            "localId": row.local_id,
            "conversationId": username,
            "conversationName": names.get(username).cloned().unwrap_or_else(|| username.to_string()),
            "isGroup": is_group,
            "senderId": sender_username,
            "senderName": names.get(&sender_username).cloned().unwrap_or(sender_username.clone()),
            "direction": direction,
            "messageType": type_name,
            "messageTypeLabel": type_label,
            "timestamp": row.create_time,
            "occurredAt": occurred_at,
            "source": "wechat-local-db",
            "platform": env::consts::OS,
        });
        if self.config.include_text {
            payload["text"] = Value::String(format_text_for_type(&type_name, &text, row.local_id));
        }
        Some(MessageCandidate {
            event_id,
            payload,
            occurred_at,
            cursor_key: cursor_key(rel_key, table_name),
            cursor: Cursor { create_time: row.create_time, local_id: row.local_id },
        })
    }

    fn username_for_table(&self, table_name: &str) -> Option<String> {
        let target = table_name.strip_prefix("Msg_")?;
        if !is_msg_table(table_name) {
            return None;
        }
        self.known_conversation_ids()
            .into_iter()
            .find(|username| format!("{:x}", md5::compute(username.as_bytes())) == target)
    }

    fn username_for_message_row(&self, row: &MessageRow, names: &BTreeMap<String, String>) -> Option<String> {
        let content = decompress_content(row.message_content.clone(), row.ct).unwrap_or_default();
        let (sender, _) = parse_message_content(&content, true);
        if !sender.is_empty() && names.contains_key(&sender) {
            Some(sender)
        } else {
            None
        }
    }
}

#[derive(Clone)]
struct MessageRow {
    local_id: i64,
    local_type: i64,
    create_time: i64,
    real_sender_id: i64,
    message_content: ValueBytes,
    ct: Option<i64>,
}

#[derive(Clone)]
enum ValueBytes {
    Bytes(Vec<u8>),
    Text(String),
    Null,
}

impl Default for ValueBytes {
    fn default() -> Self {
        Self::Null
    }
}

#[derive(Default)]
struct SessionRow {
    username: String,
    unread_count: i64,
    summary: ValueBytes,
    last_timestamp: i64,
    last_msg_type: i64,
    last_msg_sender: String,
    last_sender_display_name: String,
}

fn main() {
    if let Err(error) = run(env::args().skip(1).collect()) {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

fn run(args: Vec<String>) -> Result<(), String> {
    let parsed = ParsedArgs::parse(args);
    if parsed.flag("version") {
        println!("{VERSION}");
        return Ok(());
    }
    match parsed.command.as_str() {
        "--version" | "version" => {
            println!("{VERSION}");
            Ok(())
        }
        "init-config" => {
            let cfg = load_config_from_args(&parsed)?;
            let output = parsed.value("output").map(expand_home);
            let path = cfg.save(output)?;
            println!("wrote config: {}", path.display());
            Ok(())
        }
        "setup" => {
            let cfg = load_config_from_args(&parsed)?;
            let result = setup_collector(&cfg, parsed.flag("force"), !parsed.flag("no-extract-keys"))?;
            print_json(&result)
        }
        "probe" => {
            let cfg = load_config_from_args(&parsed)?;
            let source = WeChatSource::new(cfg)?;
            print_json(&source.probe())
        }
        "register" => {
            let cfg = load_config_from_args(&parsed)?;
            let response = BridgeClient::new(cfg).register_service(None);
            println!("{}", response.body);
            if response.ok { Ok(()) } else { Err(format!("register failed: HTTP {}", response.status)) }
        }
        "install-autostart" => {
            let cfg = load_config_from_args(&parsed)?;
            print_json(&install_autostart(&cfg)?)
        }
        "start" => {
            let cfg = load_config_from_args(&parsed)?;
            print_json(&start_collector(&cfg)?)
        }
        "status" => {
            let cfg = load_config_from_args(&parsed)?;
            let result = collector_status(&cfg);
            print_json(&result)?;
            if result.get("status").and_then(Value::as_str) == Some("running") { Ok(()) } else { Err("collector is stopped".to_string()) }
        }
        "run" => run_loop(load_config_from_args(&parsed)?, &parsed),
        other => Err(format!("unknown command: {other}")),
    }
}

#[derive(Default)]
struct ParsedArgs {
    command: String,
    values: HashMap<String, String>,
    flags: HashSet<String>,
}

impl ParsedArgs {
    fn parse(args: Vec<String>) -> Self {
        let mut parsed = Self { command: "help".to_string(), ..Default::default() };
        let mut index = 0;
        while index < args.len() {
            let arg = &args[index];
            if let Some(name) = arg.strip_prefix("--") {
                if index + 1 < args.len() && !args[index + 1].starts_with("--") {
                    parsed.values.insert(name.to_string(), args[index + 1].clone());
                    index += 2;
                } else {
                    parsed.flags.insert(name.to_string());
                    index += 1;
                }
            } else {
                if parsed.command == "help" {
                    parsed.command = arg.clone();
                }
                index += 1;
            }
        }
        parsed
    }
    fn value(&self, name: &str) -> Option<&str> { self.values.get(name).map(String::as_str) }
    fn flag(&self, name: &str) -> bool { self.flags.contains(name) }
}

fn load_config_from_args(parsed: &ParsedArgs) -> Result<CollectorConfig, String> {
    let implicit_config;
    let config_path = if parsed.value("config").is_none() {
        if let Some(state_dir) = parsed.value("state-dir") {
            implicit_config = expand_home(state_dir).join("config.json").display().to_string();
            Some(implicit_config.as_str())
        } else {
            None
        }
    } else {
        parsed.value("config")
    };
    let mut cfg = CollectorConfig::load(config_path)?;
    if let Some(value) = parsed.value("bridge-url") { cfg.bridge_base_url = value.to_string(); }
    if let Some(value) = parsed.value("event-token") { cfg.bridge_event_token = Some(value.to_string()); }
    if let Some(value) = parsed.value("service-registration-token") { cfg.service_registration_token = Some(value.to_string()); }
    if let Some(value) = parsed.value("wechat-decrypt-dir") { cfg.wechat_decrypt_dir = Some(value.to_string()); }
    if let Some(value) = parsed.value("wechat-decrypt-config") { cfg.wechat_decrypt_config = Some(value.to_string()); }
    if let Some(value) = parsed.value("db-dir") { cfg.db_dir = Some(value.to_string()); }
    if let Some(value) = parsed.value("keys-file") { cfg.keys_file = Some(value.to_string()); }
    if let Some(value) = parsed.value("state-dir") { cfg.state_dir = value.to_string(); }
    if let Some(value) = parsed.value("method-host") { cfg.method_host = value.to_string(); }
    if let Some(value) = parsed.value("method-port") { cfg.method_port = value.parse().map_err(|_| "invalid method-port".to_string())?; }
    if let Some(value) = parsed.value("poll-interval") { cfg.poll_interval_secs = value.parse().map_err(|_| "invalid poll-interval".to_string())?; }
    if let Some(value) = parsed.value("batch-size") { cfg.batch_size = value.parse().map_err(|_| "invalid batch-size".to_string())?; }
    Ok(cfg)
}

fn run_loop(mut cfg: CollectorConfig, parsed: &ParsedArgs) -> Result<(), String> {
    if parsed.flag("no-text") { cfg.include_text = false; }
    if parsed.flag("incoming-only") { cfg.include_outgoing = false; }
    let source = Arc::new(WeChatSource::new(cfg.clone())?);
    let server = QueryMethodServer::start(cfg.clone(), source.clone())?;
    let bridge = BridgeClient::new(cfg.clone());
    let mut state = CollectorState::load(&cfg.state_path());
    let first_start = !cfg.state_path().exists();

    if parsed.flag("register") {
        let response = bridge.register_service(Some(server.base_url.clone()));
        if !response.ok {
            return Err(format!("register failed: HTTP {} {}", response.status, response.body));
        }
        println!("registered bridge-agent service methods and events");
    }
    if first_start || parsed.flag("reset-state") {
        state = CollectorState::default();
        let backfill = parsed.value("backfill-seconds").and_then(|v| v.parse::<i64>().ok()).unwrap_or(0);
        source.bootstrap_state(&mut state, backfill);
        state.save(&cfg.state_path())?;
        if backfill <= 0 {
            println!("initialized state without historical broadcast: {}", cfg.state_path().display());
        }
    }
    println!("collector running service={}.{} bridge={} methods={} state={}", cfg.service_name, cfg.event_name, cfg.bridge_events_url(), server.base_url, cfg.state_path().display());

    loop {
        let (current_sessions, changed) = source.changed_usernames(&state);
        let mut emitted = 0;
        let mut failed = false;
        for candidate in source.iter_new_messages(&state, &changed, cfg.batch_size) {
            let ok = if parsed.flag("dry-run") {
                println!("{}", serde_json::to_string(&candidate.payload).map_err(|e| e.to_string())?);
                true
            } else {
                let response = bridge.emit_message(&candidate.payload, &candidate.event_id, Some(&candidate.occurred_at));
                if !response.ok {
                    eprintln!("emit failed: HTTP {} {}; state cursor was not advanced", response.status, response.body);
                    false
                } else {
                    true
                }
            };
            if !ok {
                failed = true;
                break;
            }
            state.set_cursor(candidate.cursor_key, candidate.cursor.create_time, candidate.cursor.local_id);
            emitted += 1;
        }
        if !failed {
            state.sessions = current_sessions;
        }
        state.save(&cfg.state_path())?;
        if parsed.flag("once") {
            println!("emitted={emitted} changed_sessions={}", changed.len());
            return if failed { Err("emit failed".to_string()) } else { Ok(()) };
        }
        thread::sleep(Duration::from_millis((cfg.poll_interval_secs * 1000.0) as u64));
    }
}

struct QueryMethodServer {
    base_url: String,
}

impl QueryMethodServer {
    fn start(config: CollectorConfig, source: Arc<WeChatSource>) -> Result<Self, String> {
        let addr = format!("{}:{}", config.method_host, config.method_port)
            .to_socket_addrs()
            .map_err(|e| e.to_string())?
            .next()
            .ok_or_else(|| "invalid method server address".to_string())?;
        let server = Arc::new(Server::http(addr).map_err(|e| e.to_string())?);
        let actual = server.server_addr().to_string();
        let base_url = format!("http://{actual}");
        let server_for_thread = server.clone();
        thread::spawn(move || {
            for request in server_for_thread.incoming_requests() {
                let source = source.clone();
                thread::spawn(move || {
                    handle_http_request(request, source);
                });
            }
        });
        Ok(Self { base_url })
    }
}

fn handle_http_request(mut request: Request, source: Arc<WeChatSource>) {
    let path = request.url().split('?').next().unwrap_or(request.url()).to_string();
    if request.method() == &Method::Get && path == "/health" {
        respond_json(request, 200, json!({"ok": true}));
        return;
    }
    if request.method() != &Method::Post || !path.starts_with("/invoke/") {
        respond_json(request, 404, error_response("NOT_FOUND", "unknown path"));
        return;
    }
    let method = path.trim_start_matches("/invoke/").to_string();
    let body = read_request_json(&mut request);
    let payload = match body {
        Ok(value) => value,
        Err(error) => {
            respond_json(request, 400, error_response("BAD_REQUEST", &error));
            return;
        }
    };
    match dispatch_method(&source, &method, &payload) {
        Ok(data) => respond_json(request, 200, json!({"success": true, "data": data, "error": null})),
        Err(error) => respond_json(request, 400, error_response("BAD_REQUEST", &error)),
    }
}

fn dispatch_method(source: &WeChatSource, method: &str, payload: &Value) -> Result<Value, String> {
    let obj = payload.as_object().ok_or_else(|| "请求体必须是 JSON object".to_string())?;
    match method {
        "getRecentSessions" => {
            let limit = value_usize(obj.get("limit"), 20);
            Ok(json!({"sessions": source.recent_sessions(limit), "limit": normalize_limit(limit, 200)}))
        }
        "getContacts" => {
            let limit = value_usize(obj.get("limit"), 50);
            let query = obj.get("query").and_then(Value::as_str).unwrap_or("");
            Ok(json!({"contacts": source.contacts(query, limit), "limit": normalize_limit(limit, 500)}))
        }
        "getChatHistory" => source.get_chat_history(
            require_string(obj, "conversationId")?,
            value_usize(obj.get("limit"), 50),
            value_usize(obj.get("offset"), 0),
            obj.get("startTime").or_else(|| obj.get("start_time")).unwrap_or(&Value::String(String::new())),
            obj.get("endTime").or_else(|| obj.get("end_time")).unwrap_or(&Value::String(String::new())),
            obj.get("oldestFirst").or_else(|| obj.get("oldest_first")).and_then(Value::as_bool).unwrap_or(false),
            obj.get("messageTypes").or_else(|| obj.get("message_types")),
        ),
        "searchMessages" => source.search_messages(
            require_string(obj, "keyword")?,
            obj.get("conversationId").and_then(Value::as_str).unwrap_or(""),
            value_usize(obj.get("limit"), 20),
            value_usize(obj.get("offset"), 0),
            obj.get("startTime").or_else(|| obj.get("start_time")).unwrap_or(&Value::String(String::new())),
            obj.get("endTime").or_else(|| obj.get("end_time")).unwrap_or(&Value::String(String::new())),
        ),
        "getMessageById" => Ok(json!({"message": source.get_message_by_id(require_string(obj, "messageId")?)?})),
        "getChatImages" => source.get_chat_history(require_string(obj, "conversationId")?, value_usize(obj.get("limit"), 20), value_usize(obj.get("offset"), 0), obj.get("startTime").unwrap_or(&Value::String(String::new())), obj.get("endTime").unwrap_or(&Value::String(String::new())), false, Some(&json!(["image"]))),
        "getVoiceMessages" => source.get_chat_history(require_string(obj, "conversationId")?, value_usize(obj.get("limit"), 20), value_usize(obj.get("offset"), 0), obj.get("startTime").unwrap_or(&Value::String(String::new())), obj.get("endTime").unwrap_or(&Value::String(String::new())), false, Some(&json!(["voice"]))),
        _ => Err(format!("unknown method: {method}")),
    }
}

#[derive(Clone)]
struct BridgeClient {
    config: CollectorConfig,
    client: Client,
}

#[derive(Debug)]
struct BridgeResponse {
    ok: bool,
    status: u16,
    body: String,
}

impl BridgeClient {
    fn new(config: CollectorConfig) -> Self {
        Self { config, client: Client::builder().timeout(Duration::from_secs(15)).build().unwrap_or_else(|_| Client::new()) }
    }

    fn post_json(&self, url: &str, data: &Value, token: Option<&str>) -> BridgeResponse {
        let mut request = self.client.post(url).json(data);
        if let Some(token) = token.filter(|v| !v.is_empty()) {
            request = request.bearer_auth(token);
        }
        match request.send() {
            Ok(response) => {
                let status = response.status().as_u16();
                let body = response.text().unwrap_or_default();
                BridgeResponse { ok: (200..300).contains(&status), status, body }
            }
            Err(error) => BridgeResponse { ok: false, status: 0, body: error.to_string() },
        }
    }

    fn register_service(&self, method_base_url: Option<String>) -> BridgeResponse {
        let registration = service_registration(&self.config, method_base_url.as_deref());
        self.post_json(&self.config.bridge_services_url(), &registration, self.config.service_registration_token.as_deref())
    }

    fn emit_message(&self, payload: &Value, event_id: &str, occurred_at: Option<&str>) -> BridgeResponse {
        let mut request = json!({
            "service": self.config.service_name,
            "event": self.config.event_name,
            "eventId": event_id,
            "payload": payload,
        });
        if let Some(value) = occurred_at {
            request["occurredAt"] = Value::String(value.to_string());
        }
        self.post_json(&self.config.bridge_events_url(), &request, self.config.bridge_event_token.as_deref())
    }
}

fn service_registration(config: &CollectorConfig, method_base_url: Option<&str>) -> Value {
    json!({
        "name": config.service_name,
        "description": "Local WeChat message collector.",
        "transport": {"type": "http", "baseUrl": method_base_url.unwrap_or(&config.method_base_url())},
        "healthCheck": {"type": "http", "path": "/health", "timeoutSecs": 2, "expectStatus": 200},
        "methods": method_declarations(),
        "events": [{"name": config.event_name, "description": "Emitted when a local WeChat message is observed.", "enabled": true, "payload_schema": message_event_payload_schema()}],
        "startCommand": start_command_value(),
        "replace": true,
        "managed_by": "wechat-bridge-collector",
    })
}

fn setup_collector(cfg: &CollectorConfig, force: bool, extract_keys: bool) -> Result<Value, String> {
    fs::create_dir_all(cfg.state_dir_path()).map_err(|e| e.to_string())?;
    let mut cfg = cfg.clone();
    if cfg.db_dir.is_none() {
        let runtime = cfg.load_wechat_runtime()?;
        cfg.db_dir = Some(runtime.db_dir.display().to_string());
    }
    if cfg.keys_file.is_none() {
        cfg.keys_file = Some(cfg.default_keys_path().display().to_string());
    }
    if cfg.decrypted_dir.is_none() {
        cfg.decrypted_dir = Some(cfg.default_decrypted_path().display().to_string());
    }
    cfg.save(None)?;
    let keys_path = expand_home(cfg.keys_file.as_deref().unwrap_or(""));
    if keys_path.exists() && !force {
        return Ok(json!({"status": "ready", "config_path": cfg.config_path(), "keys_file": keys_path, "db_dir": cfg.db_dir}));
    }
    if !extract_keys {
        return Ok(json!({"status": "config_written", "config_path": cfg.config_path(), "keys_file": keys_path, "db_dir": cfg.db_dir}));
    }
    extract_wechat_keys(&cfg, &keys_path)?;
    Ok(json!({"status": "keys_extracted", "config_path": cfg.config_path(), "keys_file": keys_path, "db_dir": cfg.db_dir}))
}

fn extract_wechat_keys(cfg: &CollectorConfig, output_path: &Path) -> Result<(), String> {
    if env::consts::OS == "macos" {
        let wd_dir = cfg.resolved_wechat_decrypt_dir()?;
        let source = wd_dir.join("find_all_keys_macos.c");
        if !source.is_file() {
            return Err(format!("wechat-decrypt macOS scanner source not found: {}", source.display()));
        }
        fs::create_dir_all(output_path.parent().unwrap_or_else(|| Path::new("."))).map_err(|e| e.to_string())?;
        let binary = output_path.parent().unwrap_or_else(|| Path::new(".")).join("find_all_keys_macos");
        run_checked(Command::new("cc").args(["-O2", "-o"]).arg(&binary).arg(&source).args(["-framework", "Foundation"]), 60)?;
        let _ = Command::new("codesign").args(["-s", "-"]).arg(&binary).output();
        run_checked(Command::new(&binary).current_dir(output_path.parent().unwrap_or_else(|| Path::new("."))), 180)?;
        let generated = output_path.parent().unwrap_or_else(|| Path::new(".")).join("all_keys.json");
        if !generated.is_file() {
            return Err("wechat-decrypt macOS scanner did not generate all_keys.json".to_string());
        }
        if generated != output_path {
            fs::rename(generated, output_path).map_err(|e| e.to_string())?;
        }
        return Ok(());
    }
    let wd_dir = cfg.resolved_wechat_decrypt_dir()?;
    let script = wd_dir.join("find_all_keys.py");
    if !script.is_file() {
        return Err(format!("wechat-decrypt key extraction script not found: {}", script.display()));
    }
    fs::create_dir_all(output_path.parent().unwrap_or_else(|| Path::new("."))).map_err(|e| e.to_string())?;
    let python = env::var("PYTHON").unwrap_or_else(|_| "python3".to_string());
    run_checked(Command::new(python).arg(script).current_dir(output_path.parent().unwrap_or_else(|| Path::new("."))).env("WECHAT_DECRYPT_APP_DIR", wd_dir), 180)
}

fn install_autostart(cfg: &CollectorConfig) -> Result<Value, String> {
    if env::consts::OS == "macos" {
        let plist = home_dir().join("Library/LaunchAgents/com.baijimu.wechat-bridge-collector.plist");
        fs::create_dir_all(plist.parent().unwrap()).map_err(|e| e.to_string())?;
        let exe = env::current_exe().map_err(|e| e.to_string())?;
        let stdout = cfg.state_dir_path().join("collector.log");
        let stderr = cfg.state_dir_path().join("collector.err.log");
        fs::create_dir_all(cfg.state_dir_path()).map_err(|e| e.to_string())?;
        fs::write(&plist, render_macos_plist(&exe, &cfg.config_path(), &cfg.state_dir_path(), &stdout, &stderr)).map_err(|e| e.to_string())?;
        let _ = Command::new("launchctl").args(["bootout", &format!("gui/{}", unsafe { libc_getuid() })]).arg(&plist).output();
        run_checked(Command::new("launchctl").args(["bootstrap", &format!("gui/{}", unsafe { libc_getuid() })]).arg(&plist), 15)?;
        let _ = Command::new("launchctl").args(["kickstart", "-k", &format!("gui/{}/com.baijimu.wechat-bridge-collector", unsafe { libc_getuid() })]).output();
        Ok(json!({"status": "installed", "platform": "darwin", "launcher_path": plist, "autostart_path": plist, "health_url": format!("{}/health", cfg.method_base_url())}))
    } else {
        Err(format!("install-autostart is not supported on {}", env::consts::OS))
    }
}

fn start_collector(cfg: &CollectorConfig) -> Result<Value, String> {
    if env::consts::OS == "macos" {
        let plist = home_dir().join("Library/LaunchAgents/com.baijimu.wechat-bridge-collector.plist");
        if plist.exists() {
            run_checked(Command::new("launchctl").args(["kickstart", "-k", &format!("gui/{}/com.baijimu.wechat-bridge-collector", unsafe { libc_getuid() })]), 15)?;
            return Ok(json!({"status": "started", "platform": "darwin", "launcher_path": plist, "health_url": format!("{}/health", cfg.method_base_url())}));
        }
    }
    fs::create_dir_all(cfg.state_dir_path()).map_err(|e| e.to_string())?;
    let stdout = OpenOptions::new().create(true).append(true).open(cfg.state_dir_path().join("collector.log")).map_err(|e| e.to_string())?;
    let stderr = OpenOptions::new().create(true).append(true).open(cfg.state_dir_path().join("collector.err.log")).map_err(|e| e.to_string())?;
    let exe = env::current_exe().map_err(|e| e.to_string())?;
    Command::new(exe)
        .arg("--config")
        .arg(cfg.config_path())
        .arg("run")
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok(json!({"status": "started", "platform": env::consts::OS, "health_url": format!("{}/health", cfg.method_base_url()), "message": "started without platform autostart"}))
}

fn collector_status(cfg: &CollectorConfig) -> Value {
    let url = format!("{}/health", cfg.method_base_url());
    let ok = Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .and_then(|client| client.get(&url).send())
        .map(|response| response.status().is_success())
        .unwrap_or(false);
    json!({"status": if ok { "running" } else { "stopped" }, "platform": env::consts::OS, "health_url": url})
}

fn query_session_rows(conn: &Connection, sql: &str, limit: usize) -> rusqlite::Result<Vec<SessionRow>> {
    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map(params![limit as i64], |row| {
        Ok(SessionRow {
            username: row.get(0)?,
            unread_count: row.get(1).unwrap_or(0),
            summary: value_bytes(row, 2),
            last_timestamp: row.get(3).unwrap_or(0),
            last_msg_type: row.get(4).unwrap_or(0),
            last_msg_sender: row.get(5).unwrap_or_default(),
            last_sender_display_name: row.get(6).unwrap_or_default(),
        })
    })?;
    Ok(rows.flatten().collect())
}

fn query_table_rows(conn: &Connection, table_name: &str, start_ts: Option<i64>, end_ts: Option<i64>, type_filter: Option<&HashSet<i64>>, limit: usize, oldest_first: bool) -> Vec<MessageRow> {
    let has_ct = has_column(conn, table_name, "WCDB_CT_message_content");
    let mut clauses = Vec::new();
    let mut params_values: Vec<Value> = Vec::new();
    if let Some(value) = start_ts {
        clauses.push("create_time >= ?".to_string());
        params_values.push(Value::from(value));
    }
    if let Some(value) = end_ts {
        clauses.push("create_time <= ?".to_string());
        params_values.push(Value::from(value));
    }
    if let Some(filter) = type_filter.filter(|v| !v.is_empty()) {
        clauses.push(format!("(local_type & 4294967295) IN ({})", vec!["?"; filter.len()].join(",")));
        for value in filter {
            params_values.push(Value::from(*value));
        }
    }
    let where_sql = if clauses.is_empty() { String::new() } else { format!("WHERE {}", clauses.join(" AND ")) };
    let order = if oldest_first { "ASC" } else { "DESC" };
    let sql = format!("SELECT local_id, local_type, create_time, real_sender_id, message_content, {} FROM [{}] {} ORDER BY create_time {}, local_id {} LIMIT ?", if has_ct { "WCDB_CT_message_content" } else { "NULL" }, table_name, where_sql, order, order);
    params_values.push(Value::from(normalize_limit(limit, 1000) as i64));
    let params_sql = params_values.iter().map(json_to_sql_value).collect::<Vec<_>>();
    let Ok(mut stmt) = conn.prepare(&sql) else {
        return Vec::new();
    };
    let Ok(rows) = stmt.query_map(rusqlite::params_from_iter(params_sql.iter()), message_row_from_row) else {
        return Vec::new();
    };
    rows.flatten().collect()
}

fn message_row_from_row(row: &Row<'_>) -> rusqlite::Result<MessageRow> {
    Ok(MessageRow {
        local_id: row.get(0)?,
        local_type: row.get(1).unwrap_or(0),
        create_time: row.get(2).unwrap_or(0),
        real_sender_id: row.get(3).unwrap_or(0),
        message_content: value_bytes(row, 4),
        ct: row.get(5).ok(),
    })
}

fn value_bytes(row: &Row<'_>, index: usize) -> ValueBytes {
    if let Ok(bytes) = row.get::<_, Vec<u8>>(index) {
        return ValueBytes::Bytes(bytes);
    }
    if let Ok(text) = row.get::<_, String>(index) {
        return ValueBytes::Text(text);
    }
    ValueBytes::Null
}

fn json_to_sql_value(value: &Value) -> rusqlite::types::Value {
    match value {
        Value::Number(number) => rusqlite::types::Value::Integer(number.as_i64().unwrap_or(0)),
        Value::String(text) => rusqlite::types::Value::Text(text.clone()),
        Value::Bool(value) => rusqlite::types::Value::Integer(if *value { 1 } else { 0 }),
        _ => rusqlite::types::Value::Null,
    }
}

fn load_name2id_maps(conn: &Connection) -> HashMap<i64, String> {
    let mut map = HashMap::new();
    if let Ok(mut stmt) = conn.prepare("SELECT rowid, user_name FROM Name2Id") {
        if let Ok(rows) = stmt.query_map([], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))) {
            for (id, name) in rows.flatten() {
                if !name.is_empty() {
                    map.insert(id, name);
                }
            }
        }
    }
    map
}

fn has_column(conn: &Connection, table_name: &str, column: &str) -> bool {
    let Ok(mut stmt) = conn.prepare(&format!("PRAGMA table_info([{table_name}])")) else {
        return false;
    };
    stmt.query_map([], |row| row.get::<_, String>(1))
        .map(|rows| rows.flatten().any(|name| name == column))
        .unwrap_or(false)
}

fn max_cursor(conn: &Connection, table_name: &str) -> Cursor {
    conn.query_row(&format!("SELECT create_time, local_id FROM [{table_name}] ORDER BY create_time DESC, local_id DESC LIMIT 1"), [], |row| {
        Ok(Cursor { create_time: row.get(0).unwrap_or(0), local_id: row.get(1).unwrap_or(0) })
    })
    .unwrap_or_default()
}

fn full_decrypt(db_path: &Path, out_path: &Path, enc_key: &[u8]) -> Result<(), String> {
    if let Some(parent) = out_path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let mut input = File::open(db_path).map_err(|e| e.to_string())?;
    let mut output = File::create(out_path).map_err(|e| e.to_string())?;
    let file_size = input.metadata().map_err(|e| e.to_string())?.len() as usize;
    let total_pages = file_size.div_ceil(PAGE_SZ);
    let mut page = vec![0_u8; PAGE_SZ];
    for pgno in 1..=total_pages {
        page.fill(0);
        let n = input.read(&mut page).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        let dec = decrypt_page(enc_key, &page, pgno as u32)?;
        output.write_all(&dec).map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn decrypt_wal(wal_path: &Path, out_path: &Path, enc_key: &[u8]) -> Result<(), String> {
    if !wal_path.exists() || fs::metadata(wal_path).map_err(|e| e.to_string())?.len() as usize <= WAL_HEADER_SZ {
        return Ok(());
    }
    let mut wal = File::open(wal_path).map_err(|e| e.to_string())?;
    let mut db = OpenOptions::new().read(true).write(true).open(out_path).map_err(|e| e.to_string())?;
    let mut header = [0_u8; WAL_HEADER_SZ];
    wal.read_exact(&mut header).map_err(|e| e.to_string())?;
    let wal_salt1 = u32::from_be_bytes(header[16..20].try_into().unwrap());
    let wal_salt2 = u32::from_be_bytes(header[20..24].try_into().unwrap());
    let frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ;
    let file_size = fs::metadata(wal_path).map_err(|e| e.to_string())?.len() as usize;
    while wal.stream_position().map_err(|e| e.to_string())? as usize + frame_size <= file_size {
        let mut fh = [0_u8; WAL_FRAME_HEADER_SZ];
        wal.read_exact(&mut fh).map_err(|e| e.to_string())?;
        let pgno = u32::from_be_bytes(fh[0..4].try_into().unwrap());
        let frame_salt1 = u32::from_be_bytes(fh[8..12].try_into().unwrap());
        let frame_salt2 = u32::from_be_bytes(fh[12..16].try_into().unwrap());
        let mut encrypted = vec![0_u8; PAGE_SZ];
        wal.read_exact(&mut encrypted).map_err(|e| e.to_string())?;
        if pgno == 0 || pgno > 1_000_000 || frame_salt1 != wal_salt1 || frame_salt2 != wal_salt2 {
            continue;
        }
        db.seek(SeekFrom::Start((pgno as u64 - 1) * PAGE_SZ as u64)).map_err(|e| e.to_string())?;
        db.write_all(&decrypt_page(enc_key, &encrypted, pgno)?).map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn decrypt_page(enc_key: &[u8], page: &[u8], pgno: u32) -> Result<Vec<u8>, String> {
    let iv = &page[PAGE_SZ - RESERVE_SZ..PAGE_SZ - RESERVE_SZ + IV_SZ];
    let encrypted = if pgno == 1 { &page[SALT_SZ..PAGE_SZ - RESERVE_SZ] } else { &page[..PAGE_SZ - RESERVE_SZ] };
    let mut buf = encrypted.to_vec();
    let cipher = Aes256CbcDec::new_from_slices(enc_key, iv).map_err(|e| e.to_string())?;
    let decrypted = cipher.decrypt_padded_mut::<NoPadding>(&mut buf).map_err(|e| e.to_string())?;
    let mut output = Vec::with_capacity(PAGE_SZ);
    if pgno == 1 {
        output.extend_from_slice(SQLITE_HDR);
    }
    output.extend_from_slice(decrypted);
    output.resize(PAGE_SZ, 0);
    Ok(output)
}

fn assert_sqlite_healthy(path: &Path) -> Result<(), String> {
    let conn = Connection::open(path).map_err(|e| e.to_string())?;
    let result: String = conn.query_row("PRAGMA quick_check", [], |row| row.get(0)).map_err(|e| e.to_string())?;
    if result == "ok" {
        Ok(())
    } else {
        Err(format!("SQLite quick_check failed: {result}"))
    }
}

fn find_msg_db_keys(keys: &HashMap<String, Value>) -> Vec<String> {
    let mut output = keys
        .iter()
        .filter(|(key, value)| value.get("enc_key").is_some() && is_message_db_key(key))
        .map(|(key, _)| key.clone())
        .collect::<Vec<_>>();
    output.sort();
    output
}

fn is_message_db_key(key: &str) -> bool {
    let normalized = key.replace('\\', "/");
    let Some(file_name) = normalized.strip_prefix("message/") else {
        return false;
    };
    if file_name.contains('/') {
        return false;
    }
    let Some(stem) = file_name.strip_suffix(".db") else {
        return false;
    };
    let Some(index) = stem
        .strip_prefix("message_")
        .or_else(|| stem.strip_prefix("biz_message_"))
    else {
        return false;
    };
    !index.is_empty() && index.bytes().all(|byte| byte.is_ascii_digit())
}

fn parse_time_range(start: &Value, end: &Value) -> Result<(Option<i64>, Option<i64>), String> {
    let start_ts = parse_time_value(start, false)?;
    let end_ts = parse_time_value(end, true)?;
    if let (Some(a), Some(b)) = (start_ts, end_ts) {
        if a > b {
            return Err("startTime 不能晚于 endTime".to_string());
        }
    }
    Ok((start_ts, end_ts))
}

fn parse_time_value(value: &Value, is_end: bool) -> Result<Option<i64>, String> {
    if value.is_null() {
        return Ok(None);
    }
    if let Some(n) = value.as_i64() {
        return Ok(Some(n));
    }
    if let Some(n) = value.as_f64() {
        return Ok(Some(n as i64));
    }
    let text = value.as_str().unwrap_or("").trim();
    if text.is_empty() {
        return Ok(None);
    }
    if text.chars().all(|c| c.is_ascii_digit()) {
        return text.parse::<i64>().map(Some).map_err(|e| e.to_string());
    }
    let normalized = text.replace('T', " ");
    if let Ok(dt) = NaiveDateTime::parse_from_str(&normalized, "%Y-%m-%d %H:%M:%S")
        .or_else(|_| NaiveDateTime::parse_from_str(&normalized, "%Y-%m-%d %H:%M")) {
        return Ok(Local.from_local_datetime(&dt).single().map(|d| d.timestamp()));
    }
    if let Ok(date) = NaiveDate::parse_from_str(&normalized, "%Y-%m-%d") {
        let dt = date.and_hms_opt(if is_end { 23 } else { 0 }, if is_end { 59 } else { 0 }, if is_end { 59 } else { 0 }).unwrap();
        return Ok(Local.from_local_datetime(&dt).single().map(|d| d.timestamp()));
    }
    DateTime::parse_from_rfc3339(text)
        .map(|dt| Some(dt.timestamp()))
        .map_err(|_| format!("无法解析时间: {text}"))
}

fn resolve_type_filter(value: Option<&Value>) -> Result<Option<HashSet<i64>>, String> {
    let Some(array) = value.and_then(Value::as_array) else {
        return Ok(None);
    };
    let mut codes = HashSet::new();
    let mut unknown = Vec::new();
    for item in array {
        let key = item.as_str().unwrap_or("").trim().to_lowercase();
        if key.is_empty() {
            continue;
        }
        if let Ok(value) = key.parse::<i64>() {
            codes.insert(value);
            continue;
        }
        match key.as_str() {
            "text" => { codes.insert(1); }
            "image" => { codes.insert(3); }
            "voice" => { codes.insert(34); }
            "video" => { codes.insert(43); }
            "sticker" | "emoji" => { codes.insert(47); }
            "location" => { codes.insert(48); }
            "app" | "file" => { codes.insert(49); }
            "contact_card" | "namecard" => { codes.insert(42); }
            "call" => { codes.insert(50); }
            "system" => { codes.insert(10000); }
            "recall" => { codes.insert(10002); }
            _ => unknown.push(key),
        }
    }
    if !unknown.is_empty() {
        return Err(format!("未知消息类型: {}", unknown.join(", ")));
    }
    Ok(if codes.is_empty() { None } else { Some(codes) })
}

fn parse_message_id(message_id: &str) -> Result<(String, String, i64), String> {
    let parts = message_id.rsplitn(3, ':').collect::<Vec<_>>();
    if parts.len() != 3 {
        return Err("messageId 格式不正确".to_string());
    }
    let local_id = parts[0].parse::<i64>().map_err(|_| "messageId localId 不正确".to_string())?;
    let table_name = parts[1].to_string();
    let rel_key = parts[2].to_string();
    if rel_key.is_empty() || !is_msg_table(&table_name) {
        return Err("messageId 格式不正确".to_string());
    }
    Ok((rel_key, table_name, local_id))
}

fn is_msg_table(name: &str) -> bool {
    name.len() == 36 && name.starts_with("Msg_") && name[4..].chars().all(|c| c.is_ascii_hexdigit() && c.is_ascii_lowercase() || c.is_ascii_digit())
}

fn decompress_content(content: ValueBytes, ct: Option<i64>) -> Option<String> {
    match content {
        ValueBytes::Bytes(bytes) if ct == Some(4) => zstd::decode_all(&bytes[..]).ok().and_then(|decoded| String::from_utf8(decoded).ok()),
        ValueBytes::Bytes(bytes) => Some(String::from_utf8_lossy(&bytes).to_string()),
        ValueBytes::Text(text) => Some(text),
        ValueBytes::Null => Some(String::new()),
    }
}

fn parse_message_content(content: &str, is_group: bool) -> (String, String) {
    if is_group {
        if let Some((sender, text)) = content.split_once(":\n") {
            return (sender.to_string(), text.to_string());
        }
    }
    (String::new(), content.to_string())
}

fn format_text_for_type(type_name: &str, text: &str, local_id: i64) -> String {
    match type_name {
        "image" if text.is_empty() => format!("[图片] local_id={local_id}"),
        "sticker" => "[表情]".to_string(),
        "voice" => if text.is_empty() { "[语音]".to_string() } else { text.to_string() },
        "video" => if text.is_empty() { "[视频]".to_string() } else { text.to_string() },
        "app" => summarize_app_xml(text).unwrap_or_else(|| "[链接/文件]".to_string()),
        _ if text.trim_start().starts_with('<') => summarize_app_xml(text).unwrap_or_else(|| summarize_xml_text(text).unwrap_or_else(|| "[XML消息]".to_string())),
        _ => text.to_string(),
    }
}

fn summarize_app_xml(text: &str) -> Option<String> {
    let title = extract_tag_text(text, "title");
    let desc = extract_tag_text(text, "des").or_else(|| extract_tag_text(text, "digest"));
    let app_type = extract_tag_text(text, "type");
    if app_type.as_deref() == Some("6") {
        return Some(title.map(|v| format!("[文件] {v}")).unwrap_or_else(|| "[文件]".to_string()));
    }
    match (title, desc) {
        (Some(t), Some(d)) if t != d => Some(format!("{t}\n{d}")),
        (Some(t), _) => Some(t),
        (_, Some(d)) => Some(d),
        _ => None,
    }
}

fn summarize_xml_text(text: &str) -> Option<String> {
    if text.contains("<emoji") {
        Some("[表情]".to_string())
    } else {
        None
    }
}

fn extract_tag_text(text: &str, tag: &str) -> Option<String> {
    if text.len() > 200_000 || text.to_ascii_uppercase().contains("<!DOCTYPE") || text.to_ascii_uppercase().contains("<!ENTITY") {
        return None;
    }
    let start_tag = format!("<{tag}>");
    let end_tag = format!("</{tag}>");
    let start = text.find(&start_tag)? + start_tag.len();
    let end = text[start..].find(&end_tag)? + start;
    let value = text[start..end].split_whitespace().collect::<Vec<_>>().join(" ");
    if value.is_empty() { None } else { Some(value) }
}

fn direction_for(is_group: bool, conversation_username: &str, sender_username: &str) -> &'static str {
    if is_group || sender_username.is_empty() {
        "unknown"
    } else if sender_username == conversation_username {
        "incoming"
    } else {
        "outgoing"
    }
}

fn type_label(code: i64) -> (&'static str, String) {
    match code {
        1 => ("text", "文本".to_string()),
        3 => ("image", "图片".to_string()),
        34 => ("voice", "语音".to_string()),
        42 => ("contact_card", "名片".to_string()),
        43 => ("video", "视频".to_string()),
        47 => ("sticker", "表情".to_string()),
        48 => ("location", "位置".to_string()),
        49 => ("app", "链接/文件".to_string()),
        50 => ("call", "通话".to_string()),
        10000 => ("system", "系统".to_string()),
        10002 => ("recall", "撤回".to_string()),
        other => ("unknown", format!("type={other}")),
    }
}

fn file_sig2(path: &Path) -> Option<FileSig2> {
    let meta = fs::metadata(path).ok()?;
    Some(FileSig2(mtime_secs(&meta), meta.len()))
}

fn file_sig4(db_path: &Path, wal_path: &Path) -> Option<FileSig4> {
    let db = fs::metadata(db_path).ok()?;
    let wal = fs::metadata(wal_path).ok();
    Some(FileSig4(mtime_secs(&db), db.len(), wal.as_ref().map(mtime_secs).unwrap_or(0.0), wal.as_ref().map(|m| m.len()).unwrap_or(0)))
}

fn mtime_secs(meta: &fs::Metadata) -> f64 {
    meta.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn timestamp_to_iso(timestamp: i64) -> Value {
    if timestamp <= 0 {
        Value::Null
    } else {
        Value::String(Utc.timestamp_opt(timestamp, 0).single().unwrap_or_else(Utc::now).to_rfc3339())
    }
}

fn message_sort_key(value: &Value) -> (i64, i64) {
    (value.get("timestamp").and_then(Value::as_i64).unwrap_or(0), value.get("localId").and_then(Value::as_i64).unwrap_or(0))
}

fn cursor_key(rel_key: &str, table_name: &str) -> String {
    format!("{rel_key}#{table_name}")
}

fn normalize_limit(value: usize, maximum: usize) -> usize {
    value.max(1).min(maximum)
}

fn value_usize(value: Option<&Value>, default: usize) -> usize {
    value.and_then(Value::as_u64).map(|v| v as usize).unwrap_or(default)
}

fn require_string<'a>(payload: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    payload.get(key).and_then(Value::as_str).filter(|v| !v.trim().is_empty()).map(str::trim).ok_or_else(|| format!("{key} 不能为空"))
}

fn read_request_json(request: &mut Request) -> Result<Value, String> {
    let mut body = String::new();
    request.as_reader().read_to_string(&mut body).map_err(|e| e.to_string())?;
    if body.trim().is_empty() {
        return Ok(json!({}));
    }
    let value: Value = serde_json::from_str(&body).map_err(|_| "请求体不是有效 JSON".to_string())?;
    if !value.is_object() {
        return Err("请求体必须是 JSON object".to_string());
    }
    Ok(value)
}

fn respond_json(request: Request, status: u16, payload: Value) {
    let body = serde_json::to_vec(&payload).unwrap_or_else(|_| b"{}".to_vec());
    let response = Response::from_data(body)
        .with_status_code(StatusCode(status))
        .with_header(Header::from_bytes(&b"Content-Type"[..], &b"application/json; charset=utf-8"[..]).unwrap());
    let _ = request.respond(response);
}

fn error_response(code: &str, message: &str) -> Value {
    json!({"success": false, "data": null, "error": {"code": code, "message": message}})
}

fn print_json(value: &Value) -> Result<(), String> {
    println!("{}", serde_json::to_string_pretty(value).map_err(|e| e.to_string())?);
    Ok(())
}

fn method_declarations() -> Value {
    json!([
        {"name":"getRecentSessions","description":"List recent WeChat conversations with latest-message summaries.","path":"/invoke/getRecentSessions","httpMethod":"POST","timeoutSecs":30,"input_schema":{"type":"object","properties":{"limit":{"type":"integer","minimum":1,"maximum":200,"default":20}},"additionalProperties":false}},
        {"name":"getContacts","description":"Search or list local WeChat contacts and group conversations.","path":"/invoke/getContacts","httpMethod":"POST","timeoutSecs":30,"input_schema":{"type":"object","properties":{"query":{"type":"string","default":""},"limit":{"type":"integer","minimum":1,"maximum":500,"default":50}},"additionalProperties":false}},
        {"name":"getChatHistory","description":"Read paginated message history for one WeChat conversation by conversationId.","path":"/invoke/getChatHistory","httpMethod":"POST","timeoutSecs":60,"input_schema":{"type":"object","required":["conversationId"],"properties":{"conversationId":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":500,"default":50},"offset":{"type":"integer","minimum":0,"default":0},"startTime":{"type":"string","default":""},"endTime":{"type":"string","default":""},"oldestFirst":{"type":"boolean","default":false},"messageTypes":{"type":"array","items":{"type":"string"}}},"additionalProperties":false}},
        {"name":"searchMessages","description":"Search local WeChat messages by keyword, optionally scoped to one conversation.","path":"/invoke/searchMessages","httpMethod":"POST","timeoutSecs":90,"input_schema":{"type":"object","required":["keyword"],"properties":{"keyword":{"type":"string"},"conversationId":{"type":"string","default":""},"limit":{"type":"integer","minimum":1,"maximum":500,"default":20},"offset":{"type":"integer","minimum":0,"default":0},"startTime":{"type":"string","default":""},"endTime":{"type":"string","default":""}},"additionalProperties":false}},
        {"name":"getMessageById","description":"Fetch one local WeChat message by collector messageId.","path":"/invoke/getMessageById","httpMethod":"POST","timeoutSecs":30,"input_schema":{"type":"object","required":["messageId"],"properties":{"messageId":{"type":"string"}},"additionalProperties":false}},
        {"name":"getChatImages","description":"List image messages in one WeChat conversation.","path":"/invoke/getChatImages","httpMethod":"POST","timeoutSecs":60,"input_schema":{"type":"object","required":["conversationId"],"properties":{"conversationId":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":500,"default":20},"offset":{"type":"integer","minimum":0,"default":0},"startTime":{"type":"string","default":""},"endTime":{"type":"string","default":""}},"additionalProperties":false}},
        {"name":"getVoiceMessages","description":"List voice messages in one WeChat conversation.","path":"/invoke/getVoiceMessages","httpMethod":"POST","timeoutSecs":60,"input_schema":{"type":"object","required":["conversationId"],"properties":{"conversationId":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":500,"default":20},"offset":{"type":"integer","minimum":0,"default":0},"startTime":{"type":"string","default":""},"endTime":{"type":"string","default":""}},"additionalProperties":false}}
    ])
}

fn message_event_payload_schema() -> Value {
    json!({"type":"object","description":"Payload emitted for each WeChat message observed in the local database.","required":["messageId","dbPath","tableName","localId","conversationId","conversationName","isGroup","senderId","senderName","direction","messageType","messageTypeLabel","timestamp","occurredAt","source","platform"],"properties":{"messageId":{"type":"string"},"dbPath":{"type":"string"},"tableName":{"type":"string"},"localId":{"type":"integer"},"conversationId":{"type":"string"},"conversationName":{"type":"string"},"isGroup":{"type":"boolean"},"senderId":{"type":"string"},"senderName":{"type":"string"},"direction":{"type":"string","enum":["incoming","outgoing","unknown"]},"messageType":{"type":"string","enum":["text","image","voice","contact_card","video","sticker","location","app","call","system","recall","unknown"]},"messageTypeLabel":{"type":"string"},"timestamp":{"type":"integer"},"occurredAt":{"type":"string","format":"date-time"},"source":{"type":"string","enum":["wechat-local-db"]},"platform":{"type":"string"},"text":{"type":"string"}},"additionalProperties":false})
}

fn start_command_value() -> Value {
    json!({"type": "shell_command", "command": ["wechat-bridge-collector", "start"], "timeoutSecs": 20})
}

fn render_macos_plist(exe: &Path, config: &Path, state_dir: &Path, stdout: &Path, stderr: &Path) -> String {
    format!(r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.baijimu.wechat-bridge-collector</string>
<key>ProgramArguments</key><array><string>{}</string><string>--config</string><string>{}</string><string>run</string></array>
<key>WorkingDirectory</key><string>{}</string>
<key>StandardOutPath</key><string>{}</string>
<key>StandardErrorPath</key><string>{}</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><false/>
</dict></plist>
"#, exe.display(), config.display(), state_dir.display(), stdout.display(), stderr.display())
}

fn run_checked(command: &mut Command, _timeout_secs: u64) -> Result<(), String> {
    let output = command.output().map_err(|e| e.to_string())?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!("command failed with exit code {:?}\nstdout:\n{}\nstderr:\n{}", output.status.code(), String::from_utf8_lossy(&output.stdout), String::from_utf8_lossy(&output.stderr)))
    }
}

fn is_loopback_url(value: &str) -> bool {
    value.contains("127.0.0.1") || value.contains("localhost") || value.contains("[::1]") || value.contains("::1")
}

fn load_bridge_agent_tokens() -> HashMap<String, String> {
    for path in bridge_agent_config_candidates() {
        let Ok(text) = fs::read_to_string(path) else { continue; };
        let Ok(value) = serde_json::from_str::<Value>(&text) else { continue; };
        let mut out = HashMap::new();
        for key in ["event_server_token", "service_registration_token"] {
            if let Some(token) = value.pointer(&format!("/runtime/{key}")).and_then(Value::as_str).filter(|v| !v.trim().is_empty()) {
                out.insert(key.to_string(), token.trim().to_string());
            }
        }
        if !out.is_empty() {
            return out;
        }
    }
    HashMap::new()
}

fn bridge_agent_config_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    for name in ["WS_BRIDGE_CONFIG", "BRIDGE_AGENT_CONFIG"] {
        if let Ok(value) = env::var(name) {
            if !value.is_empty() {
                candidates.push(expand_home(&value));
            }
        }
    }
    if env::consts::OS == "macos" {
        candidates.push(home_dir().join("Library/Application Support/com.baijimu.bridge-agent/agent-config.json"));
    } else if env::consts::OS == "windows" {
        if let Ok(value) = env::var("ProgramData") {
            candidates.push(PathBuf::from(value).join("Baijimu/BridgeAgent/agent-config.json"));
        }
        if let Ok(value) = env::var("APPDATA") {
            candidates.push(PathBuf::from(value).join("baijimu/bridge-agent/config/agent-config.json"));
        }
    } else {
        candidates.push(env::var("XDG_CONFIG_HOME").map(PathBuf::from).unwrap_or_else(|_| home_dir().join(".config")).join("bridge-agent/agent-config.json"));
    }
    candidates.into_iter().filter(|p| p.is_file()).collect()
}

fn auto_detect_db_dir() -> Option<String> {
    let mut candidates = Vec::new();
    if env::consts::OS == "macos" {
        let base = home_dir().join("Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files");
        candidates.extend(glob_db_storage(&base));
    } else if env::consts::OS == "linux" {
        candidates.extend(glob_db_storage(&home_dir().join("Documents/xwechat_files")));
    } else if env::consts::OS == "windows" {
        if let Ok(value) = env::var("USERPROFILE") {
            candidates.extend(glob_db_storage(&PathBuf::from(value).join("Documents/xwechat_files")));
        }
        if let Ok(value) = env::var("LOCALAPPDATA") {
            candidates.extend(glob_db_storage(&PathBuf::from(value).join("xwechat_files")));
        }
    }
    candidates.sort_by(|a, b| fs::metadata(b).and_then(|m| m.modified()).unwrap_or(SystemTime::UNIX_EPOCH).cmp(&fs::metadata(a).and_then(|m| m.modified()).unwrap_or(SystemTime::UNIX_EPOCH)));
    candidates.first().map(|p| p.display().to_string())
}

fn glob_db_storage(base: &Path) -> Vec<PathBuf> {
    let Ok(entries) = fs::read_dir(base) else {
        return Vec::new();
    };
    entries.flatten().map(|e| e.path().join("db_storage")).filter(|p| p.is_dir()).collect()
}

fn resolve_state_path(value: Option<&str>, state_dir: &Path, default_path: PathBuf) -> PathBuf {
    let path = value.map(expand_home).unwrap_or(default_path);
    if path.is_absolute() { path } else { state_dir.join(path) }
}

fn expand_home(value: &str) -> PathBuf {
    if value == "~" {
        return home_dir();
    }
    if let Some(rest) = value.strip_prefix("~/") {
        return home_dir().join(rest);
    }
    PathBuf::from(value)
}

fn home_dir() -> PathBuf {
    dirs::home_dir().unwrap_or_else(|| PathBuf::from("."))
}

fn default_state_dir() -> PathBuf { home_dir().join(".wechat-bridge-collector") }
fn default_state_dir_string() -> String { default_state_dir().display().to_string() }
fn default_bridge_base_url() -> String { DEFAULT_BRIDGE_BASE_URL.to_string() }
fn default_service_name() -> String { "wechatLocal".to_string() }
fn default_event_name() -> String { "messageReceived".to_string() }
fn default_poll_interval() -> f64 { 2.0 }
fn default_batch_size() -> usize { 200 }
fn default_method_host() -> String { DEFAULT_HOST.to_string() }
fn default_method_port() -> u16 { DEFAULT_PORT }
fn default_true() -> bool { true }
fn default_schema_version() -> i64 { 1 }

#[cfg(target_family = "unix")]
unsafe fn libc_getuid() -> u32 {
    unsafe extern "C" { fn getuid() -> u32; }
    getuid()
}

#[cfg(not(target_family = "unix"))]
unsafe fn libc_getuid() -> u32 { 0 }

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn message_database_keys_exclude_hot_indexes_and_media_databases() {
        let keys = HashMap::from([
            ("message/message_0.db".to_string(), json!({"enc_key": "00"})),
            ("message/biz_message_12.db".to_string(), json!({"enc_key": "00"})),
            ("message\\message_3.db".to_string(), json!({"enc_key": "00"})),
            ("message/message_fts.db".to_string(), json!({"enc_key": "00"})),
            ("message/media_0.db".to_string(), json!({"enc_key": "00"})),
            ("message/message_resource.db".to_string(), json!({"enc_key": "00"})),
            ("message/message_x.db".to_string(), json!({"enc_key": "00"})),
            ("message/nested/message_1.db".to_string(), json!({"enc_key": "00"})),
            ("message/message_2.db".to_string(), json!({"not_a_key": true})),
        ]);

        assert_eq!(
            find_msg_db_keys(&keys),
            vec![
                "message/biz_message_12.db".to_string(),
                "message/message_0.db".to_string(),
                "message\\message_3.db".to_string(),
            ]
        );
    }
}
