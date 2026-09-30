// The ground on the plan: an image of the terrain's heights (lighter is
// higher), one pixel per grid point, stretched over the environment.

export function terrainImage(ground) {
  const canvas = document.createElement("canvas");
  const rows = ground.heights || [[0]];
  canvas.width = rows[0].length;
  canvas.height = rows.length;
  const context = canvas.getContext("2d");
  const image = context.createImageData(canvas.width, canvas.height);
  const [r, g, b] = [1, 3, 5].map((index) => parseInt(ground.color.slice(index, index + 2), 16));
  const top = ground.max_height_m || 1;
  rows.forEach((row, y) => row.forEach((height, x) => {
    const shade = 0.72 + 0.5 * (height / top); // rows run north to south, as the image does
    const offset = (y * canvas.width + x) * 4;
    image.data.set([Math.min(255, r * shade), Math.min(255, g * shade), Math.min(255, b * shade), 255], offset);
  }));
  context.putImageData(image, 0, 0);
  return canvas.toDataURL();
}
