import React, { useState, useRef, useCallback } from 'react';
import { useSettingsStore } from '../store/settingsStore';

const SECTION_STYLES: Record<string, { color: string; bg: string; border: string }> = {
  'subject_definitions': { color: '#6366f1', bg: 'rgba(99,102,241,0.08)', border: '#6366f1' },
  'summary':             { color: '#8b5cf6', bg: 'rgba(139,92,246,0.08)',  border: '#8b5cf6' },
  'retention_analysis':  { color: '#a855f7', bg: 'rgba(168,85,247,0.08)', border: '#a855f7' },
  'detailed_description':{ color: '#d946ef', bg: 'rgba(217,70,239,0.08)', border: '#d946ef' },
  'overall_soundscape':  { color: '#06b6d4', bg: 'rgba(6,182,212,0.08)',  border: '#06b6d4' },
  'non_diegetic_music':  { color: '#ec4899', bg: 'rgba(236,72,153,0.08)', border: '#ec4899' },
};

const LABEL_STYLES: Record<string, { color: string; bg: string }> = {
  'Subject': { color: '#6366f1', bg: 'rgba(99,102,241,0.15)' },
  'Picture': { color: '#8b5cf6', bg: 'rgba(139,92,246,0.15)' },
  'Video':   { color: '#a855f7', bg: 'rgba(168,85,247,0.15)' },
  'Audio':   { color: '#ec4899', bg: 'rgba(236,72,153,0.15)' },
};

interface ParsedSection {
  name: string;
  content: string;
}

function parseSections(text: string): ParsedSection[] {
  const sectionNames = Object.keys(SECTION_STYLES);
  const lines = text.split('\n');
  const sections: ParsedSection[] = [];
  let current: ParsedSection | null = null;

  for (const line of lines) {
    const trimmed = line.trim();
    const match = trimmed.match(/^([a-z_]+)\s*:\s*(.*)$/);
    if (match && sectionNames.includes(match[1])) {
      if (current) sections.push(current);
      current = { name: match[1], content: match[2] };
    } else if (current) {
      current.content += (current.content ? '\n' : '') + line;
    }
  }
  if (current) sections.push(current);
  return sections;
}

const SHOT_STYLE = { color: '#d946ef', bg: 'rgba(217,70,239,0.15)' };
const TIME_STYLE = { color: '#06b6d4', bg: 'rgba(6,182,212,0.15)' };
const DIALOGUE_STYLE = { color: '#ec4899', bg: 'rgba(236,72,153,0.12)' };

function renderHighlightedLine(line: string): React.ReactNode {
  const parts: React.ReactNode[] = [];
  const regex = /(<(Subject|Picture|Video|Audio)\s+\d+>)|(\[Shot\s+\d+\])|(At\s+\d{1,2}:\d{2}\.\d{3})|(<d>.*?<\/d>)/g;
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  let key = 0;

  while ((m = regex.exec(line)) !== null) {
    if (m.index > lastIndex) {
      parts.push(line.slice(lastIndex, m.index));
    }
    const token = m[0];
    let style: { color: string; bg: string };
    if (m[1]) {
      const labelType = m[2];
      style = LABEL_STYLES[labelType] || { color: '#fff', bg: 'rgba(255,255,255,0.1)' };
    } else if (m[3]) {
      style = SHOT_STYLE;
    } else if (m[4]) {
      style = TIME_STYLE;
    } else {
      style = DIALOGUE_STYLE;
    }
    parts.push(
      <span
        key={key++}
        style={{ color: style.color, backgroundColor: style.bg, fontWeight: 600, borderRadius: '3px', padding: '0 2px' }}
      >
        {token}
      </span>
    );
    lastIndex = m.index + token.length;
  }
  if (lastIndex < line.length) {
    parts.push(line.slice(lastIndex));
  }
  return parts;
}

interface MinimaxPromptRefineProps {
  input: string;
  onInputChange: (v: string) => void;
  result: string;
  onResultChange: (v: string) => void;
  showRich: boolean;
  onShowRichChange: (v: boolean) => void;
}

const MinimaxPromptRefine = ({ input, onInputChange, result, onResultChange, showRich, onShowRichChange }: MinimaxPromptRefineProps) => {
  const { backend } = useSettingsStore();
  const [isRefining, setIsRefining] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const richRef = useRef<HTMLDivElement>(null);

  const handleRefine = async () => {
    if (!input.trim()) return;
    setIsRefining(true);
    setError('');
    try {
      const res = await fetch(`${backend.apiUrl}/minimax-refine/refine`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: input }),
      });
      const data = await res.json();
      if (data.status === 'success') {
        onResultChange(data.prompt);
        onInputChange(data.prompt);
        onShowRichChange(true);
      } else {
        setError(data.details || 'Refine failed.');
      }
    } catch (e: any) {
      setError(e.message || 'Request failed.');
    } finally {
      setIsRefining(false);
    }
  };

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleCopy = async () => {
    if (showRich && richRef.current) {
      await copyToClipboard(richRef.current.innerText);
    } else if (result) {
      await copyToClipboard(result);
    }
  };

  return (
    <div className="flex flex-col w-full max-w-4xl mx-auto p-8 gap-6">
      <div>
        <h2 className="text-2xl font-bold text-foreground">Minimax Prompt Refine</h2>
        <p className="text-muted-foreground mt-1">
          Paste a MiniMax-formatted prompt or describe a scenario, then refine it into a well-structured 6-section prompt.
        </p>
      </div>

      {/* Input */}
      <div className="flex flex-col gap-2">
        <label className="text-sm font-medium text-muted-foreground">Input Prompt / Scenario</label>
        <textarea
          value={input}
          onChange={(e) => onInputChange(e.target.value)}
          placeholder="Paste a MiniMax prompt or type a scenario description..."
          className="w-full h-[200px] p-4 border border-border rounded-lg bg-card text-foreground text-sm resize-y font-mono"
        />
      </div>

      {/* Actions */}
      <div className="flex items-center gap-3">
        <button
          onClick={handleRefine}
          disabled={isRefining || !input.trim()}
          className="px-6 py-2 bg-purple-600 text-white rounded hover:bg-purple-700 disabled:opacity-50 transition-colors"
        >
          {isRefining ? 'Refining...' : 'Refine'}
        </button>
        <button
          onClick={() => onShowRichChange(!showRich)}
          disabled={!input.trim() && !result}
          className="px-6 py-2 bg-success text-white rounded hover:bg-success/80 disabled:opacity-50 transition-colors"
        >
          {showRich ? 'Show Plain Text' : 'Convert to Rich Text'}
        </button>
        <button
          onClick={handleCopy}
          disabled={!input.trim() && !result}
          className="px-6 py-2 bg-muted text-foreground rounded hover:bg-muted/80 disabled:opacity-50 transition-colors"
        >
          {copied ? '✓ Copied' : 'Copy Prompt Below'}
        </button>
        {error && <span className="text-destructive text-sm">{error}</span>}
      </div>

      {/* Rich Text View */}
      {showRich && input.trim() && (
        <div className="flex flex-col gap-2">
          <label className="text-sm font-medium text-muted-foreground">
            Rich Text Editor <span className="text-xs text-muted-foreground">(editable)</span>
          </label>
          <div
            ref={richRef}
            contentEditable
            suppressContentEditableWarning
            className="w-full p-4 border border-border rounded-lg bg-card text-sm font-mono overflow-y-auto max-h-[800px] outline-none focus:ring-2 focus:ring-accent"
          >
            {parseSections(input).map((section, i) => {
              const style = SECTION_STYLES[section.name] || { color: '#fff', bg: 'rgba(255,255,255,0.05)', border: '#888' };
              return (
                <div key={i} className="mb-4 last:mb-0">
                  <div
                    className="text-xs font-bold uppercase tracking-wider mb-1 px-2 py-1 rounded"
                    style={{ color: style.color, backgroundColor: style.bg, borderLeft: `3px solid ${style.border}` }}
                  >
                    {section.name.replace(/_/g, ' ')}
                  </div>
                  <div className="pl-4 py-1 whitespace-pre-wrap text-foreground" style={{ borderLeft: `2px solid ${style.border}40` }}>
                    {section.content.split('\n').map((line, li) => (
                      <div key={li}>{renderHighlightedLine(line)}</div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}


    </div>
  );
};

export default MinimaxPromptRefine;
