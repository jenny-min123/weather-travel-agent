import { memo } from 'react';
import type { Message } from '../types';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useT } from '../i18n';
import styles from './ChatBubble.module.css';

interface Props {
  message: Message;
}

/**
 * Some LLMs (especially fast/streaming tiers) emit Markdown tables as a
 * single squashed line — the `|` row boundaries arrive without the line
 * breaks GFM needs to recognise the block as a table. Result: react-markdown
 * just renders pipes as plain text.
 *
 * The two helpers below split a "| ... | | --- | --- | | a | b |" line back
 * into one row per line — but only when the second logical row is the
 * `| --- | --- |` separator (so this can't fire on prose that happens to
 * contain a pipe). Code fences are passed through verbatim so we don't
 * mangle inline shell snippets like `ls | grep foo`.
 */
const TABLE_ROW_BOUNDARY = /\|\s+\|/g;
const TABLE_SEPARATOR_ROW = /^\|\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/;

function normalizeCompactTableLine(line: string): string {
  if (!line.includes('| |')) return line;

  const pipeIndexes = [...line.matchAll(/\|/g)]
    .map((match) => match.index ?? -1)
    .filter((index) => index >= 0);

  for (const index of pipeIndexes) {
    const table = line.slice(index);
    const normalizedTable = table.replace(TABLE_ROW_BOUNDARY, '|\n|');
    const rows = normalizedTable
      .split('\n')
      .map((row) => row.trim())
      .filter(Boolean);

    if (rows.length >= 2 && TABLE_SEPARATOR_ROW.test(rows[1])) {
      const prefix = line.slice(0, index).trimEnd();
      return prefix ? `${prefix}\n${normalizedTable}` : normalizedTable;
    }
  }

  return line;
}

function normalizeMarkdown(content: string): string {
  let inCodeFence = false;

  return content
    .split('\n')
    .map((line) => {
      if (/^\s*(```|~~~)/.test(line)) {
        inCodeFence = !inCodeFence;
        return line;
      }

      return inCodeFence ? line : normalizeCompactTableLine(line);
    })
    .join('\n');
}

export default memo(function ChatBubble({ message }: Props) {
  const { lang, t } = useT();
  const isUser = message.role === 'user';
  const meta = message.weatherMeta;

  if (!isUser && !message.content) return null;

  return (
    <div className={`${styles.row} ${isUser ? styles.userRow : styles.botRow}`}>
      {!isUser && <div className={styles.avatar}>⬡</div>}
      <div className={`${styles.bubble} ${isUser ? styles.userBubble : styles.botBubble}`}>
        {isUser ? (
          message.content
        ) : (
          <div className={`${styles.markdown} ${message.streaming ? styles.markdownStreaming : ''}`}>
            <Markdown remarkPlugins={[remarkGfm]}>{normalizeMarkdown(message.content)}</Markdown>
          </div>
        )}
        {!isUser && meta && (
          <span
            className={`${styles.metaBadge} ${
              meta.level === 'district' ? styles.metaDistrict : styles.metaCity
            } ${meta.degraded ? styles.metaDegraded : ''}`}
            title={`${t('weatherMeta.title')}${meta.displayName ? ` · ${meta.displayName}` : ''}`}
          >
            <span className={styles.metaDot} />
            {t(meta.level === 'district' ? 'weatherMeta.district' : 'weatherMeta.city')}
            {' · '}
            {t(meta.provider === 'qweather' ? 'weatherMeta.qweather' : 'weatherMeta.amap')}
            {meta.degraded && ` · ${t('weatherMeta.degraded')}`}
          </span>
        )}
        <span className={styles.time}>
          {new Date(message.timestamp).toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-US', { hour: '2-digit', minute: '2-digit' })}
        </span>
      </div>
      {isUser && <div className={`${styles.avatar} ${styles.userAvatar}`}>U</div>}
    </div>
  );
});
