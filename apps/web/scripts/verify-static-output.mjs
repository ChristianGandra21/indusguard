import { readdir, readFile } from "node:fs/promises";
import { extname, join, relative } from "node:path";

const root = new URL("../out/", import.meta.url);
const forbiddenNames = [".parquet", "goldens", "expected-paths", "cases.json"];
const textExtensions = new Set([".html", ".js", ".css", ".json", ".txt", ".xml"]);

async function walk(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  for (const entry of entries) {
    const path = join(directory, entry.name);
    if (forbiddenNames.some((marker) => entry.name.toLowerCase().includes(marker))) {
      throw new Error(`artefato proibido no frontend: ${relative(root.pathname, path)}`);
    }
    if (entry.isDirectory()) {
      await walk(path);
    } else if (textExtensions.has(extname(entry.name))) {
      const content = await readFile(path, "utf8");
      for (const marker of ["evals/corpus", "goldens/expected", "fixture/data/"]) {
        if (content.includes(marker)) {
          throw new Error(`referência proibida '${marker}' em ${relative(root.pathname, path)}`);
        }
      }
    }
  }
}

await walk(root.pathname);
process.stdout.write("frontend estático sem corpus, Parquet ou golden set\n");
