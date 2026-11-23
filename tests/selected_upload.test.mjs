import test from "node:test";
import assert from "node:assert/strict";
import { uploadSelectedFile } from "../ui/selected-upload.mjs";

test("selected browser file is sent in ordered chunks for the opened message", async () => {
  const received = [];
  const bytes = Buffer.alloc(250 * 1024, "x");
  const message = { accountId: "account", conversationId: "chat", messageId: "id" };
  let count = 0;
  const invoke = async (operation, args) => {
    assert.equal(operation, "selectedAttachment");
    if (args.action === "begin") {
      assert.equal(args.accountId, message.accountId);
      assert.equal(args.messageId, message.messageId);
      assert.equal(args.sizeBytes, bytes.length);
      return { ticket: "t", messageId: message.messageId };
    }
    assert.equal(args.ticket, "t");
    if (args.action === "chunk") {
      assert.equal(args.offset, count);
      const chunk = Buffer.from(args.bytes, "base64");
      received.push(chunk);
      count += chunk.length;
      return { ticket: "t", received: count };
    }
    assert.equal(args.action, "finish");
    return { state: "complete", ticket: "t", fileId: "file", messageId: "id" };
  };
  const file = new Blob([bytes], { type: "application/octet-stream" });
  file.name = "selected.bin";
  const reply = await uploadSelectedFile(invoke, message, file, () => {});
  assert.equal(reply.fileId, "file");
  assert.deepEqual(Buffer.concat(received), bytes);
});
