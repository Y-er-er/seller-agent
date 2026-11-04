/* 本地演示咨询。会话令牌只放在当前浏览器 sessionStorage，不传给卖家界面。 */
(function () {
  var q = new URLSearchParams(location.search);
  var base = q.get("api") || ((location.protocol === "http:" || location.protocol === "https:") ? location.origin : "http://127.0.0.1:8000");
  var sellerId = q.get("seller_id") || "default";
  var id = "", token = "", busy = false;
  try { id = sessionStorage.getItem("consult.id." + sellerId) || ""; token = sessionStorage.getItem("consult.token." + sellerId) || ""; } catch (e) { /* 禁止存储时仅保留本次页面 */ }

  function showError(message) {
    var box = document.getElementById("error");
    box.textContent = message || "";
    box.hidden = !message;
  }
  async function request(path, method, body) {
    var headers = {};
    if (token) { headers["X-Buyer-Token"] = token; }
    if (body) { headers["Content-Type"] = "application/json"; }
    var response = await fetch(base + path, { method: method || "GET", headers: headers, body: body ? JSON.stringify(body) : undefined });
    var data = await response.json();
    if (!response.ok) {
      var detail = data.detail || {};
      throw new Error(typeof detail === "string" ? detail : (detail.message || "请求失败"));
    }
    return data;
  }
  function render(items) {
    var box = document.getElementById("messages");
    box.replaceChildren();
    if (!items.length) {
      var empty = document.createElement("div"); empty.className = "empty";
      empty.textContent = "发一条消息开始咨询"; box.appendChild(empty); return;
    }
    items.forEach(function (item) {
      var div = document.createElement("div"); div.className = "message " + (item.role === "buyer" ? "buyer" : "human");
      var title = document.createElement("b"); title.textContent = item.role === "buyer" ? "我" : "客服";
      var text = document.createElement("span"); text.textContent = item.content;
      div.append(title, text); box.appendChild(div);
    });
    box.scrollTop = box.scrollHeight;
  }
  async function refresh() {
    if (!id || !token) { render([]); return; }
    try {
      var result = await request("/api/service/conversations/" + encodeURIComponent(id));
      render(result.messages || []);
      document.getElementById("message").disabled = result.status !== "open";
      document.getElementById("newConversation").hidden = result.status === "open";
      if (result.status !== "open") { showError("本次咨询已结束。可以开始新咨询。"); }
    } catch (error) { showError(error.message); }
  }
  document.getElementById("form").addEventListener("submit", async function (event) {
    event.preventDefault();
    if (busy) { return; }
    var field = document.getElementById("message");
    var text = field.value.trim();
    if (!text) { return; }
    busy = true; showError("");
    try {
      if (!id || !token) {
        var created = await request("/api/service/conversations", "POST", { seller_id: sellerId, message: text });
        id = created.conversation_id; token = created.buyer_token;
        try { sessionStorage.setItem("consult.id." + sellerId, id); sessionStorage.setItem("consult.token." + sellerId, token); } catch (e) { /* 本页仍可用 */ }
      } else {
        await request("/api/service/conversations/" + encodeURIComponent(id) + "/messages", "POST", { text: text });
      }
      field.value = "";
      await refresh();
    } catch (error) { showError(error.message); }
    finally { busy = false; }
  });
  document.getElementById("newConversation").addEventListener("click", function () {
    id = ""; token = "";
    try { sessionStorage.removeItem("consult.id." + sellerId); sessionStorage.removeItem("consult.token." + sellerId); } catch (e) { /* 本页继续可用 */ }
    document.getElementById("message").disabled = false;
    document.getElementById("newConversation").hidden = true;
    showError(""); render([]);
  });
  refresh();
  setInterval(refresh, 5000);
})();
