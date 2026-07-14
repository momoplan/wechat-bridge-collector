import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { createServer } from "node:http";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const root = resolve(__dirname, "..");
const cli = join(root, "target", "debug", "wechat-bridge-collector");

async function freePort() {
  const server = createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const port = server.address().port;
  server.close();
  await once(server, "close");
  return port;
}

async function waitForHealth(port) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/health`);
      if (response.ok) {
        return;
      }
    } catch {
      // Keep polling until the method server is ready.
    }
    await delay(50);
  }
  throw new Error("wechat collector did not become healthy");
}

async function postJson(port, path, body = {}) {
  const response = await fetch(`http://127.0.0.1:${port}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  assert.equal(response.status, 200, JSON.stringify(payload));
  assert.equal(payload.success, true, JSON.stringify(payload));
  return payload.data;
}

async function writeFixture(rootDir, port) {
  const dbDir = join(rootDir, "db_storage");
  const stateDir = join(rootDir, "state");
  const wdDir = join(rootDir, "wechat-decrypt");
  await mkdir(join(dbDir, "contact"), { recursive: true });
  await mkdir(join(dbDir, "session"), { recursive: true });
  await mkdir(join(dbDir, "message"), { recursive: true });
  await mkdir(wdDir, { recursive: true });
  await writeFile(join(wdDir, "key_utils.py"), "def strip_key_metadata(keys):\n    return keys\n", "utf8");

  const table = `Msg_${createHash("md5").update("alice").digest("hex")}`;
  const py = `
import sqlite3
from pathlib import Path
db_dir = Path(${JSON.stringify(dbDir)})
conn = sqlite3.connect(db_dir / "contact" / "contact.db")
conn.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT)")
conn.execute("INSERT INTO contact VALUES ('alice', 'Alice Nick', 'Alice Remark')")
conn.execute("INSERT INTO contact VALUES ('room@chatroom', 'Room', '')")
conn.commit(); conn.close()
conn = sqlite3.connect(db_dir / "session" / "session.db")
conn.execute("CREATE TABLE SessionTable(username TEXT, unread_count INTEGER, summary TEXT, last_timestamp INTEGER, last_msg_type INTEGER, last_msg_sender TEXT, last_sender_display_name TEXT)")
conn.execute("INSERT INTO SessionTable VALUES ('alice', 2, 'hello summary', 1780106113, 1, 'alice', 'Alice Sender')")
conn.commit(); conn.close()
conn = sqlite3.connect(db_dir / "message" / "message_0.db")
conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, 'alice')")
conn.execute("CREATE TABLE ${table}(local_id INTEGER, local_type INTEGER, create_time INTEGER, real_sender_id INTEGER, message_content TEXT)")
conn.execute("INSERT INTO ${table} VALUES (123, 1, 1780106113, 1, 'hello from alice')")
conn.execute("INSERT INTO ${table} VALUES (124, 3, 1780106114, 1, '')")
conn.commit(); conn.close()
`;
  execFileSync("python3", ["-c", py], { stdio: "inherit" });

  const keys = {
    "contact/contact.db": { plain: true, enc_key: "00".repeat(32) },
    "session/session.db": { plain: true, enc_key: "00".repeat(32) },
    "message/message_0.db": { plain: true, enc_key: "00".repeat(32) },
  };
  const keysFile = join(stateDir, "all_keys.json");
  await mkdir(stateDir, { recursive: true });
  await writeFile(keysFile, `${JSON.stringify(keys)}\n`, "utf8");

  const config = {
    state_dir: stateDir,
    wechat_decrypt_dir: wdDir,
    db_dir: dbDir,
    keys_file: keysFile,
    method_host: "127.0.0.1",
    method_port: port,
    poll_interval_secs: 60,
  };
  const configPath = join(stateDir, "config.json");
  await writeFile(configPath, `${JSON.stringify(config, null, 2)}\n`, "utf8");
  return { configPath, table };
}

test("rust setup writes collector-owned config without extracting", async () => {
  execFileSync("cargo", ["build"], { cwd: root, stdio: "inherit" });
  const temp = await mkdtemp(join(tmpdir(), "wechat-rust-setup-"));
  try {
    const wdDir = join(temp, "wechat-decrypt");
    const dbDir = join(temp, "db_storage");
    const stateDir = join(temp, "state");
    await mkdir(wdDir, { recursive: true });
    await mkdir(dbDir, { recursive: true });
    await writeFile(join(wdDir, "key_utils.py"), "", "utf8");
    const stdout = execFileSync(cli, [
      "setup",
      "--state-dir", stateDir,
      "--wechat-decrypt-dir", wdDir,
      "--db-dir", dbDir,
      "--no-extract-keys",
    ], { cwd: root, encoding: "utf8" });
    const payload = JSON.parse(stdout);
    assert.equal(payload.status, "config_written");
    const saved = JSON.parse(execFileSync("python3", ["-c", `import json; print(json.dumps(json.load(open(${JSON.stringify(join(stateDir, "config.json"))}))))`], { encoding: "utf8" }));
    assert.equal(saved.keys_file, join(stateDir, "all_keys.json"));
    assert.equal(saved.decrypted_dir, join(stateDir, "decrypted"));
    assert.equal(saved.db_dir, dbDir);
  } finally {
    await rm(temp, { recursive: true, force: true });
  }
});

test("rust method server serves WeChat query methods from local SQLite snapshots", async () => {
  execFileSync("cargo", ["build"], { cwd: root, stdio: "inherit" });
  const temp = await mkdtemp(join(tmpdir(), "wechat-rust-server-"));
  const port = await freePort();
  const { configPath, table } = await writeFixture(temp, port);
  const proc = spawn(cli, ["--config", configPath, "run"], {
    cwd: root,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, WECHAT_DECRYPT_DIR: "" },
  });

  try {
    await waitForHealth(port);

    const sessions = await postJson(port, "/invoke/getRecentSessions", { limit: 5 });
    assert.equal(sessions.sessions[0].conversationId, "alice");
    assert.equal(sessions.sessions[0].summary, "hello summary");

    const contacts = await postJson(port, "/invoke/getContacts", { query: "alice", limit: 5 });
    assert.equal(contacts.contacts[0].displayName, "Alice Remark");

    const history = await postJson(port, "/invoke/getChatHistory", { conversationId: "alice", limit: 5, oldestFirst: true });
    assert.equal(history.conversation.conversationName, "Alice Remark");
    assert.equal(history.messages[0].text, "hello from alice");
    assert.equal(history.messages[1].text, "[图片] local_id=124");

    const search = await postJson(port, "/invoke/searchMessages", { keyword: "alice", limit: 5 });
    assert.equal(search.messages[0].messageId, `message/message_0.db:${table}:123`);

    const single = await postJson(port, "/invoke/getMessageById", { messageId: `message/message_0.db:${table}:123` });
    assert.equal(single.message.senderName, "Alice Remark");
  } finally {
    proc.kill("SIGTERM");
    await Promise.race([once(proc, "exit"), delay(1000).then(() => proc.kill("SIGKILL"))]);
    await rm(temp, { recursive: true, force: true });
  }
});
