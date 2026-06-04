/// <reference types="vite/client" />

// Static asset imports — Vite serves these as the resolved URL string.
declare module "*.png" {
  const src: string;
  export default src;
}
declare module "*.svg" {
  const src: string;
  export default src;
}
