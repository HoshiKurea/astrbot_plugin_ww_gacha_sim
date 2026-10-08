import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const source = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const target = resolve(source, '..', 'pages', 'admin');
const files = ['index.html', 'style.css', 'app.js', 'pool-editor.mjs', 'portraits.mjs', 'resources.mjs'];
const check = process.argv.includes('--check');

if (!check) await mkdir(target, { recursive: true });
for (const name of files) {
  const content = await readFile(resolve(source, name));
  if (check) {
    const built = await readFile(resolve(target, name));
    if (!content.equals(built)) throw new Error(`${name} is out of date; run npm run build`);
  } else {
    await writeFile(resolve(target, name), content);
  }
}
console.log(check ? 'Plugin Page build is current.' : 'Plugin Page built to pages/admin/.');
