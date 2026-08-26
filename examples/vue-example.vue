<script setup lang="ts">
import { onBeforeUnmount, ref } from "vue";
import { VoiceInputClient } from "@lili-voice-input/browser";

const value = ref("");
const state = ref("idle");
const client = new VoiceInputClient({
  wsUrl: "ws://127.0.0.1:9100/v1/transcriptions/stream",
  fallbackUrl: "http://127.0.0.1:9100/v1/transcriptions",
  workletUrl: "http://127.0.0.1:9100/sdk/pcm-worklet.js",
});
client.on("statechange", (event) => { state.value = event.state; });
client.on("final", ({ text }) => { value.value += text; });
onBeforeUnmount(() => { void client.destroy(); });
</script>

<template>
  <textarea v-model="value" />
  <button type="button" @click="client.start()">开始录音</button>
  <button type="button" :disabled="state !== 'recording'" @click="client.stop()">停止</button>
</template>

