"use strict";

window.DashboardCharts = (() => {
  const namespace = "http://www.w3.org/2000/svg";

  function element(name, attributes = {}, text = null) {
    const node = document.createElementNS(namespace, name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    if (text !== null) node.textContent = text;
    return node;
  }

  function clear(container) { container.replaceChildren(); }

  function scale(domainStart, domainEnd, rangeStart, rangeEnd) {
    const width = domainEnd - domainStart || 1;
    return value => rangeStart + ((value - domainStart) / width) * (rangeEnd - rangeStart);
  }

  function paddedExtent(values, fallback = [0, 1], padding = .08) {
    const finite = values.filter(Number.isFinite);
    if (!finite.length) return fallback;
    let low = Math.min(...finite);
    let high = Math.max(...finite);
    if (low === high) {
      const spread = Math.abs(low) * .1 || 1;
      return [low - spread, high + spread];
    }
    const spread = (high - low) * padding;
    return [low - spread, high + spread];
  }

  function chartRoot(container, viewBox, titleText, descriptionText) {
    clear(container);
    const svg = element("svg", {viewBox, role: "img", "aria-labelledby": `${container.id}-title ${container.id}-description`});
    svg.append(
      element("title", {id: `${container.id}-title`}, titleText),
      element("desc", {id: `${container.id}-description`}, descriptionText)
    );
    container.append(svg);
    return svg;
  }

  function drawAxes(svg, dimensions, xMap, yMap, xDomain, yDomain, options = {}) {
    const {left, right, top, bottom, width, height} = dimensions;
    const plotRight = width - right;
    const plotBottom = height - bottom;
    for (let index = 0; index <= 4; index += 1) {
      const fraction = index / 4;
      const y = top + fraction * (plotBottom - top);
      const value = yDomain[1] - fraction * (yDomain[1] - yDomain[0]);
      svg.append(
        element("line", {x1: left, x2: plotRight, y1: y, y2: y, class: "chart-gridline"}),
        element("text", {x: left - 9, y: y + 4, "text-anchor": "end", class: "chart-label"}, options.yFormat ? options.yFormat(value) : value.toFixed(0))
      );
    }
    const xTicks = options.xTicks || Array.from({length: 5}, (_, index) => (
      xDomain[0] + (index / 4) * (xDomain[1] - xDomain[0])
    ));
    for (const value of xTicks) {
      const x = xMap(value);
      svg.append(element("text", {x, y: plotBottom + 20, "text-anchor": "middle", class: "chart-label"}, options.xFormat ? options.xFormat(value) : value.toFixed(0)));
    }
    svg.append(
      element("line", {x1: left, x2: plotRight, y1: plotBottom, y2: plotBottom, class: "chart-axis"}),
      element("line", {x1: left, x2: left, y1: top, y2: plotBottom, class: "chart-axis"}),
      element("text", {x: (left + plotRight) / 2, y: height - 4, "text-anchor": "middle", class: "chart-label"}, options.xLabel || ""),
      element("text", {x: 14, y: (top + plotBottom) / 2, transform: `rotate(-90 14 ${(top + plotBottom) / 2})`, "text-anchor": "middle", class: "chart-label"}, options.yLabel || "")
    );
    if (options.guides) {
      for (const guide of options.guides) {
        const coordinate = guide.axis === "x" ? xMap(guide.value) : yMap(guide.value);
        svg.append(element("line", guide.axis === "x"
          ? {x1: coordinate, x2: coordinate, y1: top, y2: plotBottom, class: "chart-focus-line"}
          : {x1: left, x2: plotRight, y1: coordinate, y2: coordinate, class: "chart-focus-line"}));
      }
    }
  }

  function accessibleList(container, items) {
    const list = document.createElement("ul");
    list.className = "dashboard-sr-only";
    for (const textValue of items) {
      const item = document.createElement("li");
      item.textContent = textValue;
      list.append(item);
    }
    container.append(list);
  }

  function scatter(container, config) {
    const width = 760;
    const height = 310;
    const dimensions = {width, height, left: 58, right: 28, top: 22, bottom: 48};
    const points = config.points.filter(point => Number.isFinite(point.x) && Number.isFinite(point.y));
    const xDomain = config.xDomain || paddedExtent(points.map(point => point.x));
    const yDomain = config.yDomain || paddedExtent(points.map(point => point.y));
    const xMap = scale(xDomain[0], xDomain[1], dimensions.left, width - dimensions.right);
    const yMap = scale(yDomain[0], yDomain[1], height - dimensions.bottom, dimensions.top);
    const svg = chartRoot(container, `0 0 ${width} ${height}`, config.title, config.description);
    drawAxes(svg, dimensions, xMap, yMap, xDomain, yDomain, config);

    const ordered = [...points].sort((left, right) => Number(left.selected) - Number(right.selected));
    for (const point of ordered) {
      const group = element("g");
      const circle = element("circle", {
        cx: xMap(point.x), cy: yMap(point.y), r: Math.max(5, Math.min(15, point.radius || 7)),
        class: `chart-point${point.selected ? " selected" : ""}`
      });
      circle.append(element("title", {}, point.detail || point.label));
      group.append(circle);
      if (point.selected || point.showLabel) {
        group.append(element("text", {
          x: xMap(point.x) + 10,
          y: yMap(point.y) - 9,
          class: "chart-team-label"
        }, point.label));
      }
      svg.append(group);
    }
    accessibleList(container, points.map(point => point.detail || `${point.label}: ${point.x}, ${point.y}`));
  }

  function pathFor(values, xMap, yMap) {
    return values.map((point, index) => `${index ? "L" : "M"}${xMap(point.x).toFixed(2)},${yMap(point.y).toFixed(2)}`).join(" ");
  }

  function line(container, config) {
    const width = 900;
    const height = 310;
    const dimensions = {width, height, left: 58, right: 36, top: 24, bottom: 48};
    const all = config.series.flatMap(series => series.values);
    const xDomain = config.xDomain || paddedExtent(all.map(point => point.x), [0, 1], 0);
    const bounds = all.flatMap(point => [point.lower ?? point.y, point.upper ?? point.y]);
    const yDomain = config.yDomain || paddedExtent(bounds, [0, 1], .12);
    const xMap = scale(xDomain[0], xDomain[1], dimensions.left, width - dimensions.right);
    const yMap = scale(yDomain[0], yDomain[1], height - dimensions.bottom, dimensions.top);
    const svg = chartRoot(container, `0 0 ${width} ${height}`, config.title, config.description);
    const xTicks = [...new Set(all.map(point => point.x))].sort((left, right) => left - right);
    drawAxes(svg, dimensions, xMap, yMap, xDomain, yDomain, {...config, xTicks});

    const band = config.series.find(series => series.band);
    if (band && band.values.length > 1) {
      const upper = band.values.map(point => ({x: point.x, y: point.upper ?? point.y}));
      const lower = [...band.values].reverse().map(point => ({x: point.x, y: point.lower ?? point.y}));
      svg.append(element("path", {d: `${pathFor(upper, xMap, yMap)} ${pathFor(lower, xMap, yMap).replace(/^M/, "L")} Z`, class: "chart-band"}));
    }
    for (const series of config.series) {
      if (!series.values.length) continue;
      svg.append(element("path", {d: pathFor(series.values, xMap, yMap), class: `chart-line${series.className ? ` ${series.className}` : ""}`}));
      for (const point of series.values) {
        const marker = element("circle", {cx: xMap(point.x), cy: yMap(point.y), r: series.className === "league" ? 3 : 4, class: `chart-point${series.className === "league" ? " league" : ""}`});
        marker.append(element("title", {}, `${series.label}, week ${point.x}: ${point.y.toFixed(1)} projected points`));
        svg.append(marker);
      }
      const finalPoint = series.values[series.values.length - 1];
      svg.append(element("text", {x: xMap(finalPoint.x) - 5, y: yMap(finalPoint.y) - 10, "text-anchor": "end", class: "chart-team-label"}, series.label));
    }
    accessibleList(container, config.series.flatMap(series => series.values.map(point => `${series.label}, week ${point.x}: ${point.y.toFixed(1)} projected points`)));
  }

  return Object.freeze({line, scatter});
})();
