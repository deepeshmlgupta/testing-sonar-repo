import xml.etree.ElementTree as ET  # nosec B405
from defusedxml.ElementTree import parse as safe_parse, iterparse as safe_iterparse, fromstring as safe_fromstring
from pathlib import Path
import re
import json

NS = {"a": "attribute", "c": "collection", "o": "object"}

class Model:
    def __init__(self, path: Path):
        self.path = path
        self.header = {}
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
            
        for line in content.splitlines():
            if line.startswith('<?PowerDesigner'):
                for match in re.finditer(r'([a-zA-Z]+)="([^"]*)"', line):
                    self.header[match.group(1)] = match.group(2)
                break
        
        sanitized_content = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', content)
        self.root = safe_fromstring(sanitized_content)

    def _defs(self, obj_type):
        """
        Real object *definitions* of a given type.

        PowerDesigner's XML uses the same element name for two different things:

            <o:Entity Id="o149"> ... </o:Entity>     a real definition
            <o:Entity Ref="o149"/>                   a pointer to that definition

        The pointer form appears everywhere a definition is referenced - inside
        relationship ends, identifier member lists, diagram symbols and so on -
        and it is an empty element with no Name, no Code and no ObjectID.

        Collecting both forms inflates every count and, worse, produces manifest
        rows for objects that do not exist. In SUBSURFACE AND WELLS, 1,398 of the
        1,708 <o:Entity> nodes are pointers; treating them as entities generated
        4,194 nameless manifest rows (38% of the manifest) which all resolved to
        the model root and overwrote each other there.

        A definition always carries an Id attribute; a pointer always carries a
        Ref attribute. Filtering on Ref is therefore exact, not heuristic.
        """
        return [e for e in self.root.findall(f".//o:{obj_type}", namespaces=NS)
                if e.get("Ref") is None]

    def extract(self, outdir: Path = None):
        entities = self._defs("Entity") + self._defs("Table")
        attrs = self._defs("EntityAttribute") + self._defs("Column")
        rels = self._defs("Relationship") + self._defs("Reference")
        idents = self._defs("Identifier") + self._defs("Key")

        c = {
            "Entity": len(entities),
            "EntityAttribute": len(attrs),
            "Relationship": len(rels),
            "Identifier": len(idents),
            "Inheritance": len(self._defs("Inheritance")),
            "LogicalDiagram": len(self._defs("LogicalDiagram")) + len(self._defs("PhysicalDiagram")),
            "Shortcut": len(self._defs("Shortcut")),
        }
        c["RelationshipJoin"] = len(self._defs("Join")) + len(self._defs("ReferenceJoin"))
        c["InheritanceLink"] = len(self._defs("InheritanceLink"))
        
        extended_texts = self.root.findall(".//a:ExtendedAttributesText", namespaces=NS)
        c["extended_attribute_values"] = 0
        for ext in extended_texts:
            if ext is not None and ext.text:
                c["extended_attribute_values"] += len([line for line in ext.text.splitlines() if '=' in line])
                
        c["inheritances_mutually_exclusive"] = len(self.root.findall(".//o:Inheritance[a:MutuallyExclusive='1']", namespaces=NS)) + \
                                               len(self.root.findall(".//o:Inheritance[a:MutuallyExclusive='true']", namespaces=NS))
        
        c["entities_with_comment"] = len(self.root.findall(".//o:Entity[a:Comment]", namespaces=NS)) + \
                                     len(self.root.findall(".//o:Table[a:Comment]", namespaces=NS))
        c["attributes_with_comment"] = len(self.root.findall(".//o:EntityAttribute[a:Comment]", namespaces=NS)) + \
                                       len(self.root.findall(".//o:Column[a:Comment]", namespaces=NS))
        
        c["entities_without_identifier"] = sum(1 for e in entities if e.find("c:Identifiers", namespaces=NS) is None and e.find("c:Keys", namespaces=NS) is None)
        
        c["attributes_untyped"] = sum(1 for a in attrs if a.find("a:DataType", namespaces=NS) is None)
        
        c["identifiers_primary"] = len(self.root.findall(".//o:Identifier[a:PrimaryIdentifier='1']", namespaces=NS)) + \
                                   len(self.root.findall(".//o:Identifier[a:PrimaryIdentifier='true']", namespaces=NS)) + \
                                   len(self.root.findall(".//o:Key[a:PrimaryKey='1']", namespaces=NS)) + \
                                   len(self.root.findall(".//o:Key[a:PrimaryKey='true']", namespaces=NS))
        
        ext_models = self._defs("TargetModel")
        external_models = [{"url": e.findtext("a:URL", namespaces=NS, default="")} for e in ext_models]

        ea_profile = {}
        for ext in extended_texts:
            if ext is not None and ext.text:
                current_profile = ""
                for line in ext.text.splitlines():
                    matches = list(re.finditer(r'\{[0-9A-F\-]+\},([^,]+),\d+=', line, re.IGNORECASE))
                    if matches:
                        last_match = matches[-1]
                        prop_name = last_match.group(1)
                        if len(matches) > 1:
                            current_profile = matches[-2].group(1)
                        if current_profile:
                            prop_name = f"{current_profile}.{prop_name}"
                        val = line[last_match.end():]
                        
                        if prop_name not in ea_profile:
                            ea_profile[prop_name] = {"coverage": "100%", "distinct_values": 0, "observed_values": []}
                        ea_profile[prop_name]["observed_values"].append(val)
        
        for k, v in ea_profile.items():
            v["distinct_values"] = len(set(v["observed_values"]))

        data = {
            "counts": c,
            "extended_attribute_profile": ea_profile,
            "external_models": external_models
        }

        if outdir is None:
            baseline = Path("baseline")
        else:
            baseline = outdir / "baseline"
            
        baseline.mkdir(parents=True, exist_ok=True)
        
        with open(baseline / "counts.json", "w") as f:
            json.dump(c, f, indent=2)
            
        with open(baseline / "extended_attributes.json", "w") as f:
            json.dump(ea_profile, f, indent=2)

        entities_list = []
        for e in entities:
            e_id = e.attrib.get("Id", "")
            if e.find("a:ObjectID", namespaces=NS) is not None:
                e_id = e.findtext("a:ObjectID", namespaces=NS)
                
            ent_dict = {
                "object_id": e_id,
                "name": e.findtext("a:Name", default="", namespaces=NS),
                "code": e.findtext("a:Code", default="", namespaces=NS),
                "extended_attributes": {}
            }
            ext_text = e.find("a:ExtendedAttributesText", namespaces=NS)
            if ext_text is not None and ext_text.text:
                current_profile = ""
                for line in ext_text.text.splitlines():
                    matches = list(re.finditer(r'\{[0-9A-F\-]+\},([^,]+),\d+=', line, re.IGNORECASE))
                    if matches:
                        last_match = matches[-1]
                        prop_name = last_match.group(1)
                        if len(matches) > 1:
                            current_profile = matches[-2].group(1)
                        if current_profile:
                            prop_name = f"{current_profile}.{prop_name}"
                        val = line[last_match.end():]
                        ent_dict["extended_attributes"][prop_name] = val
            entities_list.append(ent_dict)

        with open(baseline / "entities.json", "w") as f:
            json.dump(entities_list, f, indent=2)

        return data
