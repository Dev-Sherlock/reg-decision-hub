import React, { useEffect, useRef } from 'react';
import * as d3 from 'd3';
import { Node } from '../api';

interface DecisionTreeProps {
  nodes: Node[];
  /** The tree's root id, as reported by /tree. */
  rootId?: string;
  selectedNodeId?: string;
  onNodeClick: (node: Node) => void;
  highlightedPath?: string[];
}

export const DecisionTree: React.FC<DecisionTreeProps> = ({
  nodes,
  rootId,
  selectedNodeId,
  onNodeClick,
  highlightedPath = [],
}) => {
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Build tree hierarchy from flat nodes
  const buildTree = (nodes: Node[], rootId?: string): d3.HierarchyNode<Node> | null => {
    type TreeNode = Node & { children: Node[] };
    const nodeMap = new Map<string, TreeNode>(
      nodes.map(n => [n.id, { ...n, children: [] as Node[] }]),
    );

    // Take the root from the API rather than guessing it. Inferring it from the
    // `parent` field does not work: the root's own id ('root') is also the
    // parent string its children carry, so any "node that no one claims"
    // heuristic lands on a leaf.
    const rootEntry = rootId ? nodeMap.get(rootId) : undefined;

    nodes.forEach(node => {
      if (node.id === rootId) return;
      nodeMap.get(node.parent)?.children.push(nodeMap.get(node.id)!);
    });

    // nodes is empty until /tree resolves, and a rootId with no matching node
    // is a tree this component cannot draw. d3.hierarchy(undefined) throws,
    // which takes the whole tab down with it.
    if (!rootEntry) return null;
    return d3.hierarchy<Node>(rootEntry, d => d.children);
  };

  useEffect(() => {
    if (!svgRef.current || !containerRef.current) return;

    const rootNode = buildTree(nodes, rootId);
    if (!rootNode) return;

    const svg = d3.select(svgRef.current);
    const container = containerRef.current;
    const width = container.clientWidth;
    const height = container.clientHeight || 400;

    // Clear previous render
    svg.selectAll('*').remove();

    // Set up zoom. Panning and scaling are applied straight to the root group;
    // React state is not involved, so a drag does not re-render the component.
    const zoom = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.3, 3])
      .on('zoom', (event) => {
        g.attr('transform', event.transform.toString());
      });

    svg.call(zoom);

    // Create main group
    const g = svg.append('g');

    // Build tree layout
    const treeLayout = d3.tree<Node>()
      .nodeSize([180, 100])
      .separation((a, b) => (a.parent === b.parent ? 1.5 : 2));

    const treeData: d3.HierarchyPointNode<Node> = treeLayout(rootNode);

    // Center the tree
    const bounds = treeData.descendants().reduce(
      (acc, d) => ({
        x0: Math.min(acc.x0, d.x!),
        x1: Math.max(acc.x1, d.x!),
        y0: Math.min(acc.y0, d.y!),
        y1: Math.max(acc.y1, d.y!),
      }),
      { x0: Infinity, x1: -Infinity, y0: Infinity, y1: -Infinity }
    );

    const dx = bounds.x1 - bounds.x0;
    const dy = bounds.y1 - bounds.y0;
    const scale = Math.min(0.9, Math.min(width / dx, height / dy) * 0.8);
    const translateX = width / 2 - (bounds.x0 + bounds.x1) / 2 * scale;
    const translateY = height / 2 - (bounds.y0 + bounds.y1) / 2 * scale + 50;

    zoom.transform(svg, d3.zoomIdentity.translate(translateX, translateY).scale(scale));

    // Draw links
    g.selectAll<SVGPathElement, d3.HierarchyPointLink<Node>>('.link')
      .data(treeData.links())
      .join('path')
      .attr('class', 'link')
      .attr('d', d3.linkHorizontal<d3.HierarchyPointLink<Node>, d3.HierarchyPointNode<Node>>()
        .x(d => d.x)
        .y(d => d.y)
      );

    // Draw nodes
    const node = g.selectAll('.tree-node')
      .data(treeData.descendants())
      .join('g')
      .attr('class', d => `tree-node ${d.data.is_leaf ? 'leaf' : ''} ${d.data.id === selectedNodeId ? 'selected' : ''} ${highlightedPath.includes(d.data.id) ? 'highlighted' : ''}`)
      .attr('transform', d => `translate(${d.x},${d.y})`)
      .on('click', (event, d) => {
        event.stopPropagation();
        onNodeClick(d.data);
      });

    // Node circles
    node.append('circle')
      .attr('class', 'node-circle')
      .attr('r', d => d.data.is_leaf ? 22 : 20);

    // Node labels
    node.append('text')
      .attr('class', 'node-label')
      .attr('dy', '0.35em')
      .text(d => {
        const action = d.data.action;
        if (action === 'none') return 'ROOT';
        return action.length > 18 ? action.slice(0, 16) + '...' : action;
      });

    // Condition labels (smaller, below)
    node.filter(d => d.depth > 0 && d.data.condition !== 'true')
      .append('text')
      .attr('class', 'node-label-small')
      .attr('dy', '1.8em')
      .text(d => {
        const cond = d.data.condition;
        return cond.length > 22 ? cond.slice(0, 20) + '...' : cond;
      });

    // Owner indicator (small dot)
    node.filter(d => d.depth > 0)
      .append('circle')
      .attr('cx', d => d.data.is_leaf ? 16 : 14)
      .attr('cy', -16)
      .attr('r', 6)
      .attr('fill', d => d.data.owner === 'forecaster' ? '#3b82f6' : '#8b5cf6')
      .attr('stroke', 'white')
      .attr('stroke-width', 1.5);

  }, [nodes, rootId, selectedNodeId, highlightedPath]);

  return (
    <div ref={containerRef} className="tree-container">
      <svg ref={svgRef} className="tree-svg" />
      {nodes.length === 0 && <p className="empty-note">Loading decision tree…</p>}
    </div>
  );
};