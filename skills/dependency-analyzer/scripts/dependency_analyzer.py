#!/usr/bin/env python3
"""
Dependency analyzer implementation
"""

import os
import json
from pathlib import Path

def analyze_dependencies(project_path):
    """
    Analyze project dependencies and identify potential issues.
    
    Args:
        project_path (str): Path to the project directory
        
    Returns:
        dict: Report of dependency analysis
    """
    report = {
        "project_path": project_path,
        "dependencies": [],
        "issues": [],
        "summary": {}
    }
    
    # Check for common dependency files
    dep_files = {
        "requirements.txt": _analyze_requirements_txt,
        "package.json": _analyze_package_json,
        "pom.xml": _analyze_maven,
        "Cargo.toml": _analyze_cargo,
        "go.mod": _analyze_go_mod
    }
    
    project_path = Path(project_path)
    
    for dep_file, analyzer_func in dep_files.items():
        dep_file_path = project_path / dep_file
        if dep_file_path.exists():
            try:
                deps = analyzer_func(dep_file_path)
                report["dependencies"].extend(deps)
            except Exception as e:
                report["issues"].append({
                    "file": dep_file,
                    "error": str(e)
                })
    
    # Generate summary
    report["summary"] = {
        "total_dependencies": len(report["dependencies"]),
        "critical_issues": len([i for i in report["issues"] if "critical" in str(i).lower()]),
        "warning_issues": len([i for i in report["issues"] if "warning" in str(i).lower()])
    }
    
    return report

def _analyze_requirements_txt(file_path):
    """Analyze Python requirements.txt file"""
    dependencies = []
    with open(file_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                # Parse package name and version
                if '==' in line:
                    name, version = line.split('==', 1)
                    dependencies.append({
                        "name": name.strip(),
                        "version": version.strip(),
                        "type": "python",
                        "source": "requirements.txt"
                    })
                else:
                    dependencies.append({
                        "name": line,
                        "version": "latest",
                        "type": "python",
                        "source": "requirements.txt"
                    })
    return dependencies

def _analyze_package_json(file_path):
    """Analyze Node.js package.json file"""
    dependencies = []
    with open(file_path, 'r') as f:
        data = json.load(f)
        
    # Check dependencies
    for dep_type in ['dependencies', 'devDependencies']:
        if dep_type in data:
            for name, version in data[dep_type].items():
                dependencies.append({
                    "name": name,
                    "version": version,
                    "type": "nodejs",
                    "source": dep_type
                })
    return dependencies

def _analyze_maven(file_path):
    """Analyze Maven pom.xml file"""
    dependencies = []
    # Simplified Maven parsing - in reality would need XML parsing
    with open(file_path, 'r') as f:
        content = f.read()
        # Simple check for dependencies section
        if '<dependencies>' in content:
            dependencies.append({
                "name": "maven-dependencies",
                "version": "various",
                "type": "java",
                "source": "pom.xml"
            })
    return dependencies

def _analyze_cargo(file_path):
    """Analyze Rust Cargo.toml file"""
    dependencies = []
    with open(file_path, 'r') as f:
        content = f.read()
        # Simple check for dependencies section
        if '[dependencies]' in content:
            dependencies.append({
                "name": "cargo-dependencies",
                "version": "various",
                "type": "rust",
                "source": "Cargo.toml"
            })
    return dependencies

def _analyze_go_mod(file_path):
    """Analyze Go go.mod file"""
    dependencies = []
    with open(file_path, 'r') as f:
        content = f.read()
        # Simple check for require section
        if 'require (' in content or 'require ' in content:
            dependencies.append({
                "name": "go-dependencies",
                "version": "various",
                "type": "go",
                "source": "go.mod"
            })
    return dependencies

if __name__ == "__main__":
    # Example usage
    import sys
    if len(sys.argv) > 1:
        result = analyze_dependencies(sys.argv[1])
        print(json.dumps(result, indent=2))
    else:
        print("Usage: python dependency_analyzer.py <project_path>")