import json

def render_call_tree(call_data_path):
    with open(call_data_path, 'r') as f:
        entries = json.load(f)

    lines = []
    
    for i, entry in enumerate(entries):
        depth = entry["depth"] + 1
        content = '\n'.join(entry["content"])


        prefix = ""
        curr_depth_has_sibling = False
        
        for j in range(1, depth + 1):
            has_next_sibling = False
            for e in entries[i + 1:]:
                if e["depth"] + 1 == j:
                    has_next_sibling = True
                    break
                elif e["depth"] + 1 < j:
                    break
            if j == depth:
                curr_depth_has_sibling = has_next_sibling
            else:
                if has_next_sibling:
                    prefix += "│   "
                else:
                    prefix += "    "
                
        

        # Branch for the current call
        branch = "├── " if curr_depth_has_sibling else "└── "
        if curr_depth_has_sibling:
            curr_prefix = "│   "
        else:
            curr_prefix = "    "
            
        separator_line = prefix + "│" 
        
        # Prepare current line with correct indentation and vertical line connection
        content_with_indent = '\n'.join([prefix + branch + line if i == 0 else prefix + curr_prefix + line for i, line in enumerate(content.splitlines())])
        
        if i > 0:
            lines.append(separator_line)
        lines.append(content_with_indent)


    # Combine lines to form the final output
    return "\n".join(lines)



# if __name__ == "__main__":
#     print(render_call_tree("call_data.json"))