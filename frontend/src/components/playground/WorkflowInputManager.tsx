import React, { useState, useRef, useEffect } from 'react';
import WorkflowInputs from './WorkflowInputs';
import GenerationResult from './GenerationResult';
import { useSettingsStore } from '../../store/settingsStore';

function formatApiError(raw: string): string {
  const trimmed = raw.trim();
  try {
    const parsed = JSON.parse(trimmed);
    if (typeof parsed.detail === 'string' && parsed.detail.trim()) {
      return parsed.detail.trim();
    }
    if (typeof parsed.details === 'string' && parsed.details.trim()) {
      return parsed.details.trim();
    }
  } catch {
    // Response body is already plain text.
  }
  return trimmed || 'Generation failed';
}

interface InputField {
  type: 'string' | 'int' | 'float' | 'image' | 'audio';
  value: string | number;
}

interface PlaygroundObject {
  workflow_name: string;
  inputs: Record<string, InputField>;
  nodes: Record<string, any>;
  output_type: string;
  is_valid: boolean;
  invalid_reason?: string;
  image_node_map: Record<string, string>;
  audio_node_map?: Record<string, string>;
}

interface WorkflowInputManagerProps {
  activeWorkflowData: PlaygroundObject | null;
  baseUrl: string;
  workflowId: string | null;
  resultImage: string | null;
  queueCount: number;
  onResultImage: (url: string) => void;
  onQueueChange: (count: number) => void;
}

const WorkflowInputManager = ({
  activeWorkflowData,
  baseUrl,
  workflowId,
  resultImage,
  queueCount,
  onResultImage,
  onQueueChange,
}: WorkflowInputManagerProps) => {
  const [inputValues, setInputValues] = useState<Record<string, any>>({});
  const [resultType, setResultType] = useState<'image' | 'video'>('image');
  const [genError, setGenError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isPollingRef = useRef(false);
  const pollAgainRef = useRef(false);
  const unmountedRef = useRef(false);
  const editedRolesRef = useRef<Set<string>>(new Set());
  const { comfyui } = useSettingsStore();
  const pollInterval = comfyui.pollInterval || 4000;

  const baseUrlRef = useRef(baseUrl);
  const pollIntervalRef = useRef(pollInterval);
  const onResultImageRef = useRef(onResultImage);
  const onQueueChangeRef = useRef(onQueueChange);
  baseUrlRef.current = baseUrl;
  pollIntervalRef.current = pollInterval;
  onResultImageRef.current = onResultImage;
  onQueueChangeRef.current = onQueueChange;

  React.useEffect(() => {
    if (activeWorkflowData && activeWorkflowData.nodes) {
      const newInputs: Record<string, any> = {};
      Object.entries(activeWorkflowData.inputs).forEach(([id, field]) => {
        newInputs[id] = {
          type: field.type,
          value: field.value ?? ""
        };
      });
      setInputValues(newInputs);
      setResultType(activeWorkflowData.output_type === 'video' ? 'video' : 'image');
      editedRolesRef.current = new Set();
    }
  }, [activeWorkflowData]);

  const schedulePoll = () => {
    if (unmountedRef.current || pollTimerRef.current !== null) return;
    pollTimerRef.current = setTimeout(() => {
      pollTimerRef.current = null;
      void runPollCycleRef.current();
    }, pollIntervalRef.current);
  };

  const pollOnce = async () => {
    const activeRes = await fetch(`${baseUrlRef.current}/playground/active`);
    if (!activeRes.ok) return;

    const activeData = await activeRes.json();
    const tasks: string[] = activeData.tasks || [];
    let remaining = tasks.length;

    for (const taskId of tasks) {
      try {
        const res = await fetch(`${baseUrlRef.current}/playground/status/${taskId}`);
        if (res.status === 404) {
          remaining -= 1;
          continue;
        }

        const contentType = res.headers.get('content-type') || '';
        if (contentType.startsWith('image/') || contentType.startsWith('video/')) {
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          onResultImageRef.current(url);
          remaining -= 1;
          fetch(`${baseUrlRef.current}/playground/status/${taskId}`, { method: 'DELETE' }).catch(() => {});
          continue;
        }

        if (contentType.includes('json')) {
          const data = await res.json();
          if (data.status === 'error') {
            remaining -= 1;
            setGenError(data.details || 'Generation failed');
          }
        }
      } catch (err) {
        console.error(`[Gen] Poll error for task ${taskId}:`, err);
      }
    }

    onQueueChangeRef.current(Math.max(0, remaining));
  };

  const runPollCycle = async () => {
    if (isPollingRef.current) {
      pollAgainRef.current = true;
      return;
    }
    isPollingRef.current = true;
    try {
      do {
        pollAgainRef.current = false;
        await pollOnce();
      } while (pollAgainRef.current);
    } catch (err) {
      console.error('[Gen] Poll cycle error:', err);
    } finally {
      isPollingRef.current = false;
      schedulePoll();
    }
  };

  const runPollCycleRef = useRef(runPollCycle);
  runPollCycleRef.current = runPollCycle;

  useEffect(() => {
    unmountedRef.current = false;
    void runPollCycleRef.current();
    return () => {
      unmountedRef.current = true;
      if (pollTimerRef.current) {
        clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, []);

  const handleGenerate = async () => {
    if (!activeWorkflowData || !activeWorkflowData.is_valid || isSubmitting) return;
    setGenError('');

    const fileRoles = Object.entries(inputValues).filter(([_, value]) =>
      value?.type === 'image' || value?.type === 'audio'
    );

    const sortedFileRoles = [...fileRoles].sort((a, b) => {
      const titleA = activeWorkflowData.nodes[a[0]]?._meta?.title || "";
      const titleB = activeWorkflowData.nodes[b[0]]?._meta?.title || "";
      return (parseInt(titleA.toLowerCase().replace(/\D/g, '')) || 0) -
             (parseInt(titleB.toLowerCase().replace(/\D/g, '')) || 0);
    });

    // Validate: if workflow has image inputs, at least one must be edited
    const hasImageInputs = Object.values(activeWorkflowData.inputs).some(f => f.type === 'image');
    const editedImageCount = sortedFileRoles.filter(([role]) =>
      inputValues[role]?.type === 'image' && editedRolesRef.current.has(role) && inputValues[role]?.value
    ).length;

    if (hasImageInputs && editedImageCount === 0) {
      alert('Please select at least one image.');
      return;
    }

    // Validate edited inputs have values
    for (const [role, _] of sortedFileRoles) {
      if (!editedRolesRef.current.has(role)) continue;
      if (!inputValues[role]?.value) {
        const label = inputValues[role]?.type === 'audio' ? 'audio' : 'image';
        alert(`Please select a ${label} for the "${role}" input.`);
        return;
      }
    }

    const finalInputs: Record<string, any> = {};

    setIsSubmitting(true);
    try {
      for (const [role, data] of Object.entries(inputValues)) {
        if (data?.type === 'image' || data?.type === 'audio') {
          // Only include image/audio inputs that were actually edited
          if (!editedRolesRef.current.has(role)) continue;

          const nodeMap = data.type === 'image'
            ? activeWorkflowData.image_node_map
            : activeWorkflowData.audio_node_map;
          const nodeId = nodeMap?.[role] || role;

          if (data.value instanceof File) {
            const formData = new FormData();
            formData.append('file', data.value);

            const res = await fetch(`${baseUrl}/comfyui/upload`, {
              method: 'POST',
              body: formData,
            });

            if (!res.ok) {
              const errorText = await res.text();
              setGenError(formatApiError(errorText) || `Upload failed for "${role}"`);
              return;
            }

            const resultData = await res.json();
            setInputValues(prev => ({ ...prev, [role]: { type: data.type, value: resultData.name } }));
            finalInputs[role] = [nodeId, resultData.name];
          } else {
            finalInputs[role] = [nodeId, data.value];
          }
        } else {
          finalInputs[role] = data.value;
        }
      }

      const payload = {
        workflow_id: workflowId,
        inputs: finalInputs
      };

      const response = await fetch(`${baseUrl}/playground/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const errorText = await response.text();
        setGenError(formatApiError(errorText));
        return;
      }

      const data = await response.json();
      console.log(`[Gen] Queued: task_id=${data.task_id}`);
      if (pollTimerRef.current) {
        clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      void runPollCycleRef.current();
    } catch (error: any) {
      setGenError(error?.message || 'Failed to queue generation');
    } finally {
      setIsSubmitting(false);
    }
  };

  if (!activeWorkflowData) {
    return <p className="text-muted-foreground italic">Select a workflow to begin.</p>;
  }

  const hasSeed = 'seed' in activeWorkflowData.inputs;
  const hasWidth = 'width' in activeWorkflowData.inputs;
  const hasHeight = 'height' in activeWorkflowData.inputs;
  const floatInputs = Object.entries(activeWorkflowData.inputs).filter(([_, f]) => f.type === 'float');
  const seedValue = inputValues['seed']?.value ?? 0;
  const widthValue = inputValues['width']?.value ?? 960;
  const heightValue = inputValues['height']?.value ?? 544;

  const RESOLUTIONS = [
    { name: 'Square (512x512)', w: 512, h: 512 },
    { name: 'Square (768x768)', w: 768, h: 768 },
    { name: 'Square (1024x1024)', w: 1024, h: 1024 },
    { name: 'Landscape (1024x768)', w: 1024, h: 768 },
    { name: 'Landscape (1366x768)', w: 1366, h: 768 },
    { name: 'Landscape (960x544)', w: 960, h: 544 },
    { name: 'Landscape (1920x1080)', w: 1920, h: 1080 },
    { name: 'Landscape (2560x1440)', w: 2560, h: 1440 },
    { name: 'Landscape (3840x2160)', w: 3840, h: 2160 },
    { name: 'Portrait (768x1024)', w: 768, h: 1024 },
    { name: 'Portrait (1080x1366)', w: 1080, h: 1366 },
    { name: 'Portrait (544x960)', w: 544, h: 960 },
    { name: 'Portrait (1080x1920)', w: 1080, h: 1920 },
    { name: 'Portrait (1440x2560)', w: 1440, h: 2560 },
    { name: 'Portrait (2160x3840)', w: 2160, h: 3840 },
  ];

  const handleResolutionChange = (index: number) => {
    const res = RESOLUTIONS[index];
    setInputValues(prev => ({
      ...prev,
      'width': { type: 'int', value: res.w },
      'height': { type: 'int', value: res.h },
    }));
  };

  return (
    <div className="flex flex-col gap-4">
      {genError && (
        <div className="p-3 border border-destructive/40 bg-destructive/10 rounded-lg text-destructive text-sm">
          {genError}
          <button onClick={() => setGenError('')} className="ml-2 text-xs underline">Dismiss</button>
        </div>
      )}
      {/* Preview + Controls */}
      <div className="flex gap-4">
        <div className="flex-1">
          <GenerationResult
            isGenerating={queueCount > 0}
            queueCount={queueCount}
            resultImage={resultImage}
            resultType={resultType}
            onGenerate={handleGenerate}
            isValid={activeWorkflowData.is_valid}
          />
        </div>
        <div className="w-56 flex flex-col gap-3">
          {hasSeed && (
            <div className="flex flex-col gap-1">
              <label className="text-xs font-semibold uppercase text-muted-foreground">Seed</label>
              <div className="flex gap-2">
                <input
                  type="number"
                  className="p-2 border border-border rounded bg-card text-foreground text-sm w-full"
                  value={seedValue}
                  onChange={(e) => setInputValues(prev => ({ ...prev, 'seed': { type: 'int', value: e.target.value === '' ? 0 : parseInt(e.target.value) } }))}
                />
                <button
                  type="button"
                  onClick={() => setInputValues(prev => ({ ...prev, 'seed': { type: 'int', value: Math.floor(Math.random() * 1000000) } }))}
                  className="px-2 py-1 text-xs bg-muted text-muted-foreground border border-border rounded hover:bg-muted/80 transition-colors whitespace-nowrap"
                >
                  Random
                </button>
              </div>
            </div>
          )}
          {floatInputs.length > 0 && (
            <div className="flex gap-2">
              {floatInputs.map(([role]) => (
                <div key={role} className="flex flex-col gap-1 flex-1">
                  <label className="text-xs font-semibold uppercase text-muted-foreground">{role}</label>
                  <input
                    type="number"
                    step="0.1"
                    className="p-2 border border-border rounded bg-card text-foreground text-sm w-full"
                    value={inputValues[role]?.value ?? 0}
                    onChange={(e) => setInputValues(prev => ({ ...prev, [role]: { type: 'float', value: parseFloat(e.target.value) || 0 } }))}
                  />
                </div>
              ))}
            </div>
          )}
          {(hasWidth || hasHeight) && (
            <div className="flex flex-col gap-1">
              <label className="text-xs font-semibold uppercase text-muted-foreground">Resolution</label>
              <select
                className="p-2 border border-border rounded bg-card text-foreground text-sm"
                value={RESOLUTIONS.find(r => r.w === widthValue && r.h === heightValue)?.name || 'Custom'}
                onChange={(e) => {
                  const idx = RESOLUTIONS.findIndex(r => r.name === e.target.value);
                  if (idx >= 0) handleResolutionChange(idx);
                }}
              >
                {RESOLUTIONS.map((res, i) => (
                  <option key={i} value={res.name}>{res.name}</option>
                ))}
              </select>
              <div className="flex gap-2">
                {hasWidth && (
                  <div className="flex flex-col gap-0.5 flex-1">
                    <label className="text-[10px] text-muted-foreground">W</label>
                    <input
                      type="number"
                      className="p-1 border border-border rounded bg-card text-foreground text-sm w-full"
                      value={widthValue}
                      onChange={(e) => setInputValues(prev => ({ ...prev, 'width': { type: 'int', value: parseInt(e.target.value) || 0 } }))}
                    />
                  </div>
                )}
                {hasHeight && (
                  <div className="flex flex-col gap-0.5 flex-1">
                    <label className="text-[10px] text-muted-foreground">H</label>
                    <input
                      type="number"
                      className="p-1 border border-border rounded bg-card text-foreground text-sm w-full"
                      value={heightValue}
                      onChange={(e) => setInputValues(prev => ({ ...prev, 'height': { type: 'int', value: parseInt(e.target.value) || 0 } }))}
                    />
                  </div>
                )}
              </div>
            </div>
          )}
          <button
            type="button"
            onClick={handleGenerate}
            disabled={!activeWorkflowData.is_valid || isSubmitting}
            className="px-4 py-2 rounded font-bold text-white transition-all bg-accent hover:scale-105 shadow-lg disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:scale-100"
          >
            {isSubmitting ? 'Queuing...' : queueCount > 0 ? 'Queue Another' : 'Generate'}
          </button>
        </div>
      </div>

      <WorkflowInputs
        inputs={inputValues}
        values={inputValues}
        nodes={activeWorkflowData.nodes}
        baseUrl={baseUrl}
        onChange={(nodeId, value) => {
          editedRolesRef.current.add(nodeId);
          setInputValues(prev => ({ ...prev, [nodeId]: { type: prev[nodeId]?.type ?? 'string', value } }));
        }}
      />
    </div>
  );
};

export default WorkflowInputManager;
