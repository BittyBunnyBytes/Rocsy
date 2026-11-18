// Audio worklets for the Live voice panel (realtime.js). A file, not a blob: the site's CSP allows
// scripts from 'self' only.

// Microphone -> main thread, 128 samples per call (the main thread batches them into 20 ms frames).
class RocsyCapture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
}

// Converted audio from the server -> speakers / the chosen output device. A ring buffer: plays
// silence when empty, and drops the oldest audio when more than half a second is queued.
class RocsyPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = sampleRate * 2;
    this.buffer = new Float32Array(this.size);
    this.read = 0;
    this.write = 0;
    this.queued = 0;
    this.limit = Math.round(sampleRate * 0.5);
    this.port.onmessage = ({data}) => {
      if (data === 'clear') { this.read = this.write = this.queued = 0; return; }
      for (let i = 0; i < data.length; i++) {
        this.buffer[this.write] = data[i];
        this.write = (this.write + 1) % this.size;
      }
      this.queued = Math.min(this.size, this.queued + data.length);
      if (this.queued > this.limit) {
        const drop = this.queued - this.limit;
        this.read = (this.read + drop) % this.size;
        this.queued -= drop;
      }
    };
  }
  process(_inputs, outputs) {
    const out = outputs[0][0];
    for (let i = 0; i < out.length; i++) {
      if (this.queued > 0) {
        out[i] = this.buffer[this.read];
        this.read = (this.read + 1) % this.size;
        this.queued--;
      } else {
        out[i] = 0;
      }
    }
    return true;
  }
}

registerProcessor('rocsy-capture', RocsyCapture);
registerProcessor('rocsy-player', RocsyPlayer);
