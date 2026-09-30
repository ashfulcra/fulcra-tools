export default {
  test: {
    environment: "node",
    include: ["src/**/*.test.js", "scripts/**/*.test.js"],
    expect: { requireAssertions: true },
  },
};
