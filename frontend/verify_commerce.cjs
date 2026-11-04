"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const source = fs.readFileSync(path.join(__dirname, "commerce.js"), "utf8");
const ctx = vm.createContext({});
vm.runInContext(source, ctx);

function check(name, condition) {
  if (!condition) { throw new Error(name); }
  process.stdout.write("[PASS] " + name + "\n");
}

const csv = "order_id,line_id,goods_no,sku_id,title,quantity,unit_price_cent,paid_cent,status,placed_at\n"
  + 'A,2,G-2,,"标题,带逗号",1,200,200,paid,2026-10-21 10:00:00\n'
  + "A,1,G-1,,商品一,2,100,100,paid,2026-10-21 10:00:00\n";
const orders = ctx.ordersFromCsv(csv);
check("一单多行汇总", orders.length === 1 && orders[0].lines.length === 2 && orders[0].paid_cent === 300);
check("引号内逗号保留", orders[0].lines[0].title === "标题,带逗号");
let rejected = false;
try { ctx.ordersFromCsv(csv.replace("paid,2026", "shipped,2026")); } catch (e) { rejected = true; }
check("同单状态冲突被拒绝", rejected);

const chineseCsv = "订单号,下单时间,商品名称,数量,单价(元),商品金额(元),运费(元),优惠金额(元),实付金额(元),订单状态,收货地址\n"
  + "C-1,2026-07-01 10:00:00,示例商品,2,10.50,21.00,3.00,1.25,22.75,待付款,示例地址\n";
const chinese = ctx.ordersFromCsv(chineseCsv)[0];
check("中文订单金额按元转分且待付款已收为零", chinese.order_amount_cent === 2275 && chinese.paid_cent === 0);
check("中文原始字段保留", chinese.import_payload[0]["收货地址"] === "示例地址");
check("中文订单行金额与订单一致", chinese.lines[0].paid_cent === chinese.paid_cent);
rejected = false;
try { ctx.ordersFromCsv(chineseCsv.replace("22.75", "22.76")); } catch (e) { rejected = true; }
check("中文金额不平被拒绝", rejected);

ctx.escapeHtml = (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;");
check("完成、退款、取消状态有独立醒目标签",
  ctx.orderStatusTag("已完成").includes("commerce-status done")
  && ctx.orderStatusTag("已退款").includes("commerce-status refunded")
  && ctx.orderStatusTag("已取消").includes("commerce-status cancelled"));
check("订单详情有收起按键", ctx.orderDetail({
  source: "imported", external_order_id: "C-1", status: "已完成",
  paid_cent: 100, order_amount_cent: 100, lines: [], events: []
}).includes('data-act="collapse-right"'));
const withTracking = ctx.orderLogistics({
  status: "运输中", placed_at: "2026-07-01", imported_at: "2026-10-21",
  import_payload: [{ "物流公司": "中通快递", "物流单号": "T123" }]
});
check("物流卡显示运单与非实时说明", withTracking.includes("中通快递")
  && withTracking.includes("T123") && withTracking.includes("运输中") && withTracking.includes("非实时轨迹"));
check("无物流时不生成虚构节点", ctx.orderLogistics({ status: "已取消", import_payload: [] })
  .includes("尚无运单信息"));
ctx.commerceState.orders = [
  { id: 1, external_order_id: "A", status: "运输中", has_tracking: 1 },
  { id: 2, external_order_id: "B", status: "待付款", has_tracking: 0 }
];
check("列表区分有物流与暂无运单", ctx.orderBody().includes("有物流")
  && ctx.orderBody().includes("暂无运单"));
ctx.commerceState.orders = [];

(async function checkOrderDetailOpening() {
  let rendered = false;
  ctx.state = { rightOpen: false };
  ctx.api = async () => ({ ok: true, data: { id: 7, external_order_id: "C-1" } });
  ctx.persistRight = () => {};
  ctx.renderRight = () => { rendered = true; };
  const row = { getAttribute: (name) => name === "data-id" ? "7" : "select-order" };
  const target = { closest: () => row };
  ctx.handleCommerceMidClick({ target, detail: 1 }, "orders");
  check("单击订单不打开详情", !rendered && ctx.commerceState.order === null);
  ctx.handleCommerceMidDoubleClick({ target }, "orders");
  await new Promise((resolve) => setImmediate(resolve));
  check("双击订单打开详情", rendered && ctx.commerceState.order.id === 7);
  rendered = false;
  ctx.commerceState.order = null;
  ctx.handleCommerceMidClick({ target, detail: 0 }, "orders");
  await new Promise((resolve) => setImmediate(resolve));
  check("键盘激活订单仍可打开详情", rendered && ctx.commerceState.order.id === 7);
  rendered = false;
  ctx.commerceState.order = null;
  await ctx.selectCommerceOrder(7);
  check("选择订单会打开已收起的详情栏", ctx.state.rightOpen && rendered && ctx.commerceState.order.id === 7);
  check("列表明确提示查看详情", ctx.orderBody.toString().includes("查看详情"));
})().catch((err) => { process.stderr.write(String(err) + "\n"); process.exitCode = 1; });
