"use client";
/**
 * Bring-your-own media for identities, voices and worlds (cutover §4). Every control uploads through
 * the shared flow (`uploadAsset`: waits until the server validated the file) and sends the
 * attestation the API requires:
 * - a face or voice must not be a real, identifiable person — real people need the digital-twin
 *   consent path, which is not available yet (V1);
 * - a plate must show no identifiable people, and the uploader must hold its rights.
 * The server re-checks size, duration and sample rate (`uploads.references`) and records the
 * attestation on the asset; approvals refuse an uploaded reference without it.
 */
import { useMutation } from "@tanstack/react-query";
import { useId, useState } from "react";

import { ErrorNote } from "@/components/studio/common";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { type UploadStage, uploadAsset } from "@/lib/upload";

export const FACE_ATTESTATION = "not_a_real_person" as const;
export const PLATE_ATTESTATION = "no_identifiable_people_rights_held" as const;
export const VOICE_ATTESTATION = "synthetic_voice_not_a_person" as const;

const STAGE_TEXT: Record<UploadStage, string> = {
  uploading: "Uploading…",
  validating: "Validating the file…",
  ready: "Uploaded.",
};

export const ATTESTATION_TEXT = {
  face:
    "This image is a synthetic or licensed character design. It is not a photo of a real, identifiable person " +
    "(real people need the digital-twin consent flow, which is not available yet).",
  plate: "This image shows no identifiable people, and I hold the rights to use it.",
  voice:
    "This recording is synthetic (made with a voice-design or text-to-speech tool). It is not a recording of a " +
    "real person's voice (cloning a real voice needs consent, which is not available yet).",
} as const;

/** A file input, the attestation checkbox and an upload button; `onUploaded` gets the ready asset id. */
export function UploadReference({
  label,
  accept,
  kind,
  attestation,
  hint,
  disabled,
  onUploaded,
  submitLabel = "Upload",
}: {
  label: string;
  accept: string;
  kind: string;
  attestation: keyof typeof ATTESTATION_TEXT;
  hint?: string;
  disabled?: boolean;
  onUploaded: (assetId: string) => Promise<unknown>;
  submitLabel?: string;
}) {
  const id = useId();
  const [file, setFile] = useState<File | null>(null);
  const [attested, setAttested] = useState(false);
  const [stage, setStage] = useState<UploadStage | null>(null);
  const [inputKey, setInputKey] = useState(0);
  const upload = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("choose a file");
      const asset = await uploadAsset(file, { kind, onStage: setStage });
      await onUploaded(asset.id);
    },
    onSuccess: () => {
      setFile(null);
      setAttested(false);
      setInputKey((k) => k + 1); // a file input cannot be cleared; remount it
    },
    onSettled: () => setStage(null),
  });
  return (
    <div className="flex flex-col gap-2 rounded border border-slate-200 p-3">
      <Label htmlFor={`${id}-file`}>{label}</Label>
      {hint ? <p className="text-xs text-slate-600">{hint}</p> : null}
      <Input
        key={inputKey}
        id={`${id}-file`}
        type="file"
        accept={accept}
        disabled={disabled || upload.isPending}
        onChange={(e) => setFile(e.target.files?.[0] ?? null)}
      />
      <label className="flex items-start gap-2 text-sm" htmlFor={`${id}-attest`}>
        <input
          id={`${id}-attest`}
          type="checkbox"
          className="mt-1"
          checked={attested}
          disabled={disabled || upload.isPending}
          onChange={(e) => setAttested(e.target.checked)}
        />
        <span>{ATTESTATION_TEXT[attestation]}</span>
      </label>
      <div className="flex items-center gap-2">
        <Button
          type="button"
          onClick={() => upload.mutate()}
          disabled={disabled || !file || !attested || upload.isPending}
        >
          {submitLabel}
        </Button>
        {stage ? (
          <span className="text-sm text-slate-700" role="status">
            {STAGE_TEXT[stage]}
          </span>
        ) : null}
      </div>
      <ErrorNote error={upload.error} />
    </div>
  );
}

/** Voice import: the recording, its exact transcript and language, then `POST /v1/voices:from-upload`. */
export function VoiceImport({
  onImport,
}: {
  onImport: (input: { assetId: string; name: string; language: string; transcript: string }) => Promise<unknown>;
}) {
  const id = useId();
  const [name, setName] = useState("");
  const [language, setLanguage] = useState("en-US");
  const [transcript, setTranscript] = useState("");
  const ready = name.trim() !== "" && language.trim().length >= 2 && transcript.trim() !== "";
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-name`}>Name</Label>
          <Input id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-lang`}>Language</Label>
          <Input id={`${id}-lang`} className="w-28" value={language} onChange={(e) => setLanguage(e.target.value)} />
        </div>
        <div className="flex min-w-72 flex-1 flex-col gap-1">
          <Label htmlFor={`${id}-transcript`}>Transcript (exactly what the recording says)</Label>
          <Input id={`${id}-transcript`} value={transcript} onChange={(e) => setTranscript(e.target.value)} />
        </div>
      </div>
      <UploadReference
        label="Reference recording (WAV, MP3 or FLAC; 5–60 s of clean speech, at least 16 kHz)"
        accept="audio/wav,audio/x-wav,audio/mpeg,audio/flac"
        kind="audio"
        attestation="voice"
        disabled={!ready}
        submitLabel="Upload and create the voice"
        onUploaded={(assetId) => onImport({ assetId, name: name.trim(), language: language.trim(), transcript })}
      />
    </div>
  );
}
