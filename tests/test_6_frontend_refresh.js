/**
 * v7 前端验收：用真实 Vue 3 运行时在 jsdom 中跑 static/index.html，
 * 重点验证「刷新品名匹配」按钮的两个自研增强点：
 *   1. 作用于【全部已加载店铺】（而非仅当前店铺）
 *   2. 保留【手动改名】的品名，且改回自动名后徽标自动消失
 *
 * 运行：node tests/test_6_frontend_refresh.js
 * 依赖：jsdom（受管 node 工作区）+ tests/_vendor/vue.global.prod.js
 */
const fs = require('fs');
const path = require('path');

const BASE = path.dirname(__dirname);
const HTML_PATH = path.join(BASE, 'static', 'index.html');
const VUE_PATH = path.join(BASE, 'tests', '_vendor', 'vue.global.prod.js');

let OK = 0, FAIL = 0;
const check = (name, cond, detail = '') => {
  if (cond) { OK++; console.log(`  [PASS] ${name} ${detail}`); }
  else { FAIL++; console.log(`  [FAIL] ${name} ${detail}`); }
};

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  console.error('需要 jsdom：在受管 node 工作区执行 npm install jsdom');
  process.exit(2);
}

// ---------------------------------------------------------------- 造数据
const SHOPS = {
  '三头牛-JP': {
    median_price: 2000,
    total_parents: 2,
    products: [
      { parent_sku: 'mj355', product_name: '压泥器', raw_name: 'FryWare 2個 マッシャー ステンレス製',
        child_count: 3, avg_price: 1980, median_ratio: 0.99, image_url: '', selected: true, assigned_budget: 100 },
      { parent_sku: 'llc16', product_name: '塑料袋', raw_name: 'FryWare ビニール袋 鮮度保持袋',
        child_count: 1, avg_price: 1500, median_ratio: 0.75, image_url: '', selected: true, assigned_budget: 100 },
    ],
  },
  '二当家-JP': {
    median_price: 3000,
    total_parents: 1,
    products: [
      { parent_sku: 'k001', product_name: '折叠桌', raw_name: '折りたたみテーブル 大型',
        child_count: 2, avg_price: 3200, median_ratio: 1.07, image_url: '', selected: true, assigned_budget: 100 },
    ],
  },
};

// 模拟服务端词库（刷新后应得到的结果）
const REMATCH_TABLE = {
  '三头牛-JP||mj355': '压泥器',
  '三头牛-JP||llc16': '塑料袋',
  '二当家-JP||k001': '折叠桌',
};

const fetchLog = [];

function makeFetch() {
  return async (url, opts = {}) => {
    const u = String(url);
    fetchLog.push(u);
    const json = (obj, ok = true) => ({
      ok,
      status: ok ? 200 : 400,
      json: async () => obj,
      blob: async () => new Blob([JSON.stringify(obj)]),
    });

    if (u.endsWith('/api/dictionary/stats')) return json({ total_count: 362 });
    if (u.endsWith('/api/dictionary')) return json({ count: 362, blacklist_size: 49, path: 'data/ja_zh_dict.json', entries: [] });
    if (u.endsWith('/api/analyze')) return json({ shops: JSON.parse(JSON.stringify(SHOPS)), header_row: 0, resolved_columns: {} });
    if (u.endsWith('/api/dictionary/rematch')) {
      const body = JSON.parse(opts.body || '{}');
      const matched = {}, hit = [], miss = [];
      (body.items || []).forEach(it => {
        const v = REMATCH_TABLE[it.key];
        if (v) { matched[it.key] = v; hit.push(it.key); }
        else { matched[it.key] = '兜底名'; miss.push(it.key); }
      });
      return json({ matched_names: matched, hit_skus: hit, miss_skus: miss,
                    matched_count: Object.keys(matched).length, hit_count: hit.length,
                    miss_count: miss.length, dict_total: 362 });
    }
    return json({ detail: 'unhandled ' + u }, false);
  };
}

// ---------------------------------------------------------------- 构造 jsdom
const html = fs.readFileSync(HTML_PATH, 'utf8');
const vueSrc = fs.readFileSync(VUE_PATH, 'utf8').replace(/<\/script/gi, '<\\/script');
if (/<\/script/i.test(fs.readFileSync(VUE_PATH, 'utf8'))) {
  console.log('  [WARN] vue 源内出现 </script，已转义');
}

// 去掉 CDN 外链，内联本地 Vue；并在 Vue 之后、应用脚本之前插入 createApp 包装钩子
const HOOK = `<script>(function(){
  var orig = Vue.createApp;
  Vue.createApp = function(){
    var app = orig.apply(this, arguments);
    var om = app.mount;
    app.mount = function(sel){ var vm = om.call(this, sel); window.__vm = vm; return vm; };
    return app;
  };
})();</script>`;

const patched = html
  .replace(/<script src="https:\/\/cdn\.tailwindcss\.com"><\/script>/, '')
  // 注意：必须用「函数式替换器」——vueSrc 内含 `$'` / `$&` 等序列，
  // 用字符串替换会被 String.replace 当作替换模式解释，导致源码被截断。
  .replace(/<script src="https:\/\/unpkg\.com\/vue@3\/dist\/vue\.global\.prod\.js"><\/script>/,
           () => `<script>${vueSrc}</script>${HOOK}`);

if (patched.includes('unpkg.com/vue@3')) {
  console.log('  [FAIL] Vue CDN 标签未被替换，测试环境构造失败');
  process.exit(1);
}
if (!patched.includes('window.__vm')) {
  console.log('  [FAIL] createApp 钩子未注入');
  process.exit(1);
}

const dom = new JSDOM(patched, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8000/',
  beforeParse(win) { win.fetch = makeFetch(); },
});

const { window } = dom;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  console.log('='.repeat(78));
  console.log('v7 前端验收：刷新品名匹配（真实 Vue 运行时 + jsdom）');
  console.log('='.repeat(78));

  const vm = window.__vm;
  if (!vm) { console.log('  [FAIL] 未能获取 Vue 组件实例'); process.exit(1); }
  await sleep(50);

  const findRefreshBtn = () =>
    [...window.document.querySelectorAll('button')].find(b => b.textContent.includes('刷新品名匹配'));

  console.log('\n[1] 顶部词库控制栏');
  const btns = [...window.document.querySelectorAll('button')].map(b => b.textContent.trim());
  check('存在「刷新品名匹配」按钮', btns.some(t => t.includes('刷新品名匹配')), JSON.stringify(btns.filter(t => t.includes('刷新') || t.includes('导入'))));
  check('存在「导入对照表 JSON」按钮', btns.some(t => t.includes('导入对照表')));
  check('未加载表格时按钮禁用', findRefreshBtn().disabled === true);

  console.log('\n[2] 解析后打 _auto_name 基线');
  // analyzeFile 是闭包内函数（未在 setup 返回列表中），走已导出的 onDrop 入口
  await vm.onDrop({ dataTransfer: { files: [new window.File(['dummy'], 'real.xlsx')] } });
  await sleep(50);
  const shops = vm.allShopsData;
  check('两个店铺均载入', Object.keys(shops).length === 2, JSON.stringify(Object.keys(shops)));
  const p355 = shops['三头牛-JP'].products.find(p => p.parent_sku === 'mj355');
  const p16 = shops['三头牛-JP'].products.find(p => p.parent_sku === 'llc16');
  const pk = shops['二当家-JP'].products.find(p => p.parent_sku === 'k001');
  check('_auto_name 已记录', p355._auto_name === '压泥器' && p16._auto_name === '塑料袋', `${p355._auto_name}/${p16._auto_name}`);
  check('_shop 已标记', p355._shop === '三头牛-JP' && pk._shop === '二当家-JP');
  check('初始无「手动」标记', vm.isNameManuallyEdited(p355) === false);
  check('加载表格后按钮启用', findRefreshBtn().disabled === false);

  console.log('\n[3] 手动改名 -> 徽标出现');
  p355.product_name = '手动改的压泥器';
  await sleep(30);
  check('isNameManuallyEdited 判定为 true', vm.isNameManuallyEdited(p355) === true);
  const badges = [...window.document.querySelectorAll('span')].filter(s => s.textContent.trim() === '手动');
  check('DOM 中出现「手动」徽标', badges.length === 1, `${badges.length} 个`);
  const editedInput = [...window.document.querySelectorAll('input')]
    .find(i => i.value === '手动改的压泥器');
  check('输入框变琥珀色边框', !!editedInput && editedInput.className.includes('border-amber-400'),
        editedInput ? editedInput.className.slice(0, 60) : '未找到输入框');

  console.log('\n[4] 点击刷新：全店铺生效 + 保留手动改名');
  // 制造「词库已更新、页面还是旧值」的场景：把自动名与当前值一起置为过期值
  p16.product_name = '旧塑料袋'; p16._auto_name = '旧塑料袋';
  pk.product_name = '旧折叠桌';  pk._auto_name = '旧折叠桌';
  await sleep(20);
  check('过期值不算手动改名', vm.isNameManuallyEdited(p16) === false);
  const before = fetchLog.length;
  await vm.refreshNameMatching();
  await sleep(40);
  check('调用了 /api/dictionary/rematch', fetchLog.slice(before).some(u => u.includes('/rematch')));
  check('手动改名的 mj355 被保留', p355.product_name === '手动改的压泥器', p355.product_name);
  check('当前店铺 llc16 被更新', p16.product_name === '塑料袋', p16.product_name);
  check('【全店铺】另一店铺 k001 也被更新', pk.product_name === '折叠桌', pk.product_name);
  check('刷新后 _auto_name 基线同步', p16._auto_name === '塑料袋' && pk._auto_name === '折叠桌',
        `${p16._auto_name}/${pk._auto_name}`);
  check('提示含更新数与保留数', String(vm.toast).includes('更新 2 个') && String(vm.toast).includes('保留手动改名 1 个'),
        vm.toast);

  console.log('\n[5] 改回自动名 -> 徽标自动消失');
  p16.product_name = '别的名字';
  await sleep(20);
  check('llc16 变为手动状态', vm.isNameManuallyEdited(p16) === true);
  p16.product_name = p16._auto_name;
  await sleep(20);
  check('改回自动名后恢复非手动', vm.isNameManuallyEdited(p16) === false);

  console.log('\n[6] 未收录商品：回填兜底名并计数');
  const saved = REMATCH_TABLE['二当家-JP||k001'];
  delete REMATCH_TABLE['二当家-JP||k001'];   // 让服务端判定为「未收录」
  await vm.refreshNameMatching();
  await sleep(40);
  check('未收录回填兜底名', pk.product_name === '兜底名', pk.product_name);
  check('提示含「未收录」', String(vm.toast).includes('未收录'), vm.toast);
  REMATCH_TABLE['二当家-JP||k001'] = saved;

  console.log('\n[7] 无表格时点刷新给出提示');
  vm.allShopsData = {};
  await vm.refreshNameMatching();
  await sleep(20);
  check('空数据时提示先上传', String(vm.toast).includes('请先上传'), vm.toast);

  console.log('\n' + '='.repeat(78));
  console.log(`结果：PASS ${OK}  FAIL ${FAIL}`);
  console.log('='.repeat(78));

  window.close();
  process.exit(FAIL ? 1 : 0);
})().catch(e => {
  console.error('测试异常：', e);
  process.exit(1);
});
