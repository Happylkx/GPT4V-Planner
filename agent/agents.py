import json
import re
from typing import List, Optional, Dict, Any

from PIL import Image

from api.language_model import LanguageModel
from api.detectors import Detector, COMMON_OBJECTS
from api.segmentors import Segmentor

from .visualizer import GenericMask

from .utils import resize_image, visualize_bboxes, visualize_masks


DEFAULT_ACTION_SPACE = """
 - pick(object)
 - place(object, orientation). 
   - `orientation` in ['inside', 'on_top_of', 'left', 'right', 'up', 'down']
 - open(object)
"""

class PlanResult:
    def __init__(
        self, 
        success: bool = False, 
        error_message: Optional[str] = None, 
        plan_raw: Optional[str] = None, 
        masks: Optional[list[Any]] = None, 
        prompt: Optional[str] = None, 
        plan_code: Optional[str] = None, 
        annotated_image: Optional[Image.Image] = None, 
        info_dict: Optional[Dict[str, Any]] = None
    ) -> None:
        self.success = success
        self.error_message = error_message
        self.plan_raw = plan_raw
        self.masks = masks
        self.prompt = prompt
        self.plan_code = plan_code
        self.annotated_image = annotated_image
        self.info_dict = info_dict if info_dict is not None else {}

    def __repr__(self) -> str:
        return ("PlanResult("
                f"success={self.success},\n "
                f"error_message={repr(self.error_message)},\n "
                f"plan_raw={repr(self.plan_raw)},\n "
                f"masks={self.masks},\n "
                f"prompt={repr(self.prompt)},\n "
                f"plan_code={repr(self.plan_code)},\n "
                f"annotated_image={self.annotated_image},\n "
                f"info_dict={repr(self.info_dict)}"
                ")"
        )

def extract_plans_and_regions(text: str, regions: list):
    # Extract code blocks. We assume there is only one code block in the generation
    code_blocks = re.findall(r'```python(.*?)```', text, re.DOTALL)
    if not code_blocks:
        return None, None

    code_block = code_blocks[0]

    # Use regular expression to find all occurrences of region[index]
    matches = re.findall(r'regions\[(\d+)\]', code_block)

    used_indices = list(set(int(index) for index in matches))
    used_indices.sort()

    index_mapping = {old_index: new_index for new_index, old_index in enumerate(used_indices)}
    for old_index, new_index in index_mapping.items():
        code_block = code_block.replace(f'regions[{old_index}]', f'regions[{new_index}]')
    try:
        filtered_regions = [regions[index] for index in used_indices]
    except IndexError as e:  # Invalid index is used
        return None, None

    return code_block, filtered_regions

class Agent():
    def __init__(self, action_space: str = DEFAULT_ACTION_SPACE) -> None:
        self.action_space = action_space


class SegVLM(Agent):
    meta_prompt = \
'''
You are in charge of controlling a robot. You will be given a list of operations you are allowed to perform, along with a task to solve. You will see an image captured by thte robot's camera, in which some objects are highlighted with masks and marked with numbers. Output your plan as code.

Operation list:
{action_space}

Note:
- For any item mentioned in your answer, please use the format of `regions[number]`.
- Do not define the operations or regions in your code. They will be provided in the python environment.
- Your code should be surrounded by a python code block "```python".
'''

    def __init__(self, 
        segmentor: Segmentor, 
        vlm: LanguageModel,
        configs: dict = None,
        **kwargs
    ):
        if not isinstance(segmentor, Segmentor):
            raise TypeError("`segmentor` must be an instance of Segmentor.")
        if not isinstance(vlm, LanguageModel):
            raise TypeError("`vlm` must be an instance of LanguageModel.")

        self.segmentor = segmentor
        self.vlm = vlm


        # Default configs
        self.configs = {
            "img_size": 640,
            "label_mode": "1",
            "alpha": 0.05
        }
        if configs is not None:
            self.configs = self.configs.update(configs)  

        super().__init__(**kwargs)


    def plan(self, prompt: str, image: Image.Image):
        # Resize the image if necessary
        processed_image = image
        if "img_size" in self.configs:
            processed_image = resize_image(image, self.configs["img_size"])
        # Generate segmentation masks
        masks = self.segmentor.segment_auto_mask(processed_image)

        # Draw masks
        # sorted_masks = sorted(masks, key=(lambda x: x['area']), reverse=True)
        annotated_img = visualize_masks(processed_image, 
                            annotations=[anno["segmentation"] for anno in masks],
                            label_mode=self.configs["label_mode"],
                            alpha=self.configs["alpha"],
                            draw_mask=False, 
                            draw_mark=True, 
                            draw_box=False
        )
        
        plan_raw = self.vlm.chat(
            prompt=prompt, 
            image=annotated_img, 
            meta_prompt=self.meta_prompt.format(action_space=self.action_space)
        )
        
        plan_code, filtered_masks = extract_plans_and_regions(plan_raw, masks)
        if plan_code is None:
            return PlanResult(
                success=False, 
                error_message="Invalid or no code is generated.",
                plan_raw=plan_raw,
                annotated_image=annotated_img,
                prompt=prompt,
                info_dict=dict(configs=self.configs)
            )

        return PlanResult(
            success=True,
            plan_code=plan_code,
            masks=filtered_masks,
            plan_raw=plan_raw,
            annotated_image=annotated_img,
            prompt=prompt,
            info_dict=dict(configs=self.configs)
        )


class DetVLM(Agent):
    meta_prompt = \
'''
You are in charge of controlling a robot. You will be given a list of operations you are allowed to perform, along with a task to solve. You will see an image captured by thte robot's camera, in which some objects are highlighted with bounding boxes and marked with numbers. Output your plan as code.

Operation list:
{action_space}


Note:
- For any item mentioned in your answer, please use the format of `regions[number]`.
- Do not define the operations or regions in your code. They will be provided in the python environment.
- Your code should be surrounded by a python code block "```python".
'''

    def __init__(
            self, 
            detector: Detector, 
            segmentor: Segmentor,
            vlm: LanguageModel,
            configs: dict = None,
            **kwargs
            ):
        if not isinstance(detector, Detector):
            raise TypeError("`detector` must be an instance of Detector.")
        if not isinstance(segmentor, Segmentor):
            raise TypeError("`segmentor` must be an instance of Segmentor.")
        if not isinstance(vlm, LanguageModel):
            raise TypeError("`vlm` must be an instance of LanguageModel.")

        self.detector = detector
        self.segmentor = segmentor
        self.vlm = vlm

        # Default configs
        self.configs = {
            "img_size": 640,
            "label_mode": "1",
            "alpha": 0.75
        }
        if configs is not None:
            self.configs = self.configs.update(configs)            

        super().__init__(**kwargs)
    
    def plan(self, prompt: str, image: Image.Image):
        # Resize the image if necessary
        processed_image = image
        if "img_size" in self.configs:
            processed_image = resize_image(image, self.configs["img_size"])
        
        # Generate detection boxes
        text_queries = COMMON_OBJECTS
        detected_objects = self.detector.detect_objects(
            processed_image,
            text_queries,
            bbox_score_top_k=20,
            bbox_conf_threshold=0.3
        )
        #  Example result:
        # [{'score': 0.3141017258167267,
        # 'bbox': [0.212062269449234,
        # 0.3956533372402191,
        # 0.29010745882987976,
        # 0.08735490590333939],
        # 'box_name': 'roof',
        # 'objectness': 0.09425540268421173}, ...
        # ]
        if len(detected_objects) == 0:
            return PlanResult(
                success=False, 
                error_message="No objects were detected in the image.",
                info_dict=dict(objects_to_detect=text_queries)
            )

        # Draw masks
        annotated_img = visualize_bboxes(
            processed_image,
            bboxes=[obj['bbox'] for obj in detected_objects], 
            alpha=self.configs["alpha"]
        )
        
        
        plan_raw = self.vlm.chat(
            prompt=prompt, 
            image=annotated_img, 
            meta_prompt=self.meta_prompt.format(action_space=self.action_space)
        )
        masks = self.segmentor.segment_by_bboxes(image=image, bboxes=[[obj['bbox']] for obj in detected_objects])

        plan_code, filtered_masks = extract_plans_and_regions(plan_raw, masks)

        if plan_code is None:
            return PlanResult(
                success=False, 
                error_message="Invalid or no code is generated.",
                plan_raw=plan_raw,
                annotated_image=annotated_img,
                prompt=prompt,
                info_dict=dict(configs=self.configs)
            )
        
        return PlanResult(
            success=True,
            plan_code=plan_code,
            masks=filtered_masks,
            plan_raw=plan_raw,
            annotated_image=annotated_img,
            prompt=prompt,
            info_dict=dict(configs=self.configs)
        )


class DetLLM(Agent):
    meta_prompt = \
'''
You are in charge of controlling a robot. You will be given a list of operations you are allowed to perform, along with a task to solve. You will be given a list of objects detected which you may want to interact with. Output your plan as code.

Operation list:
{action_space}

Note:
- For any item referenced in your code, please use the format of `object="object_name"`.
- Do not define the operations in your code. They will be provided in the python environment.
- Your code should be surrounded by a python code block "```python".
'''
    def __init__(
            self, 
            detector: Detector,
            segmentor: Segmentor, 
            llm: LanguageModel,
            configs: dict = None,
            **kwargs
            ):
        if not isinstance(detector, Detector):
            raise TypeError("`detector` must be an instance of Detector.")
        if not isinstance(segmentor, Segmentor):
            raise TypeError("`segmentor` must be an instance of Segmentor.")
        if not isinstance(llm, LanguageModel):
            raise TypeError("`llm` must be an instance of LanguageModel.")

        self.detector = detector
        self.segmentor = segmentor
        self.llm = llm

        # Default configs
        self.configs = {
            "img_size": 640,
            "include_coordinates": True
        }
        # Configs
        if configs is not None:
            self.configs = self.configs.update(configs)


        super().__init__(**kwargs)

    def textualize_detections(self, detected_objects: list, include_coordinates=False) -> str:
        """
        Creates a Markdown formatted list of detected object names, with an option to include normalized position coordinates.

        Args:
            detected_objects (list of dict): A list of dictionaries, each representing a detected object.
                                            Each dictionary should have a 'box_name' key, and optionally a 'box' key with normalized coordinates (ranging from 0 to 1).
            include_coordinates (bool): If True, includes the positions (normalized coordinates) of the detected objects in the list, if available.

        Returns:
            str: A Markdown formatted string listing the detected object names, optionally with their normalized position coordinates.

        Example:
            Sample input:
                example_detections = [
                    {'box_name': 'Cat', 'box': [0.1, 0.15, 0.2, 0.25]},
                    {'box_name': 'Dog', 'box': [0.3, 0.35, 0.4, 0.45]},
                    {'box_name': 'Bird', 'box': [0.05, 0.075, 0.12, 0.145]},
                    {'box_name': 'Car', 'box': [0.5, 0.55, 0.6, 0.65]}
                ]
                markdown_list = textualize_detections(example_detections, include_coordinates=True)

            Sample output:
                - Cat (coordinates: (0.1, 0.15), (0.2, 0.25))
                - Dog (coordinates: (0.3, 0.35), (0.4, 0.45))
                - Bird (coordinates: (0.05, 0.075), (0.12, 0.145))
                - Car (coordinates: (0.5, 0.55), (0.6, 0.65))
        """

        markdown_list = []
        if include_coordinates:
            markdown_list.append("List of objects detected (coordinates are in (x1,y1), (x2, y2) order):")
        else:
            markdown_list.append("List of objects detected:")

        for obj in detected_objects:
            box_name = obj['box_name']
            if include_coordinates:
                box = obj['bbox']
                box_coords = f" (coordinates: ({box[0]:.2f}, {box[1]:.2f}), ({box[2]:.2f}, {box[3]:.2f}))"
                markdown_list.append(f"- {box_name}{box_coords}")
            else:
                markdown_list.append(f"- {box_name}")

        return '\n'.join(markdown_list)

    def plan(self, prompt: str, image: Image.Image):
        # Resize the image if necessary
        processed_image = image
        if "img_size" in self.configs:
            processed_image = resize_image(image, self.configs["img_size"])
        
        # Generate detection boxes
        text_queries = COMMON_OBJECTS
        detected_objects = self.detector.detect_objects(
            processed_image,
            text_queries,
            bbox_score_top_k=20,
            bbox_conf_threshold=0.5
        )
        #  Example result:
        # [{'score': 0.3141017258167267,
        # 'bbox': [0.212062269449234,
        # 0.3956533372402191,
        # 0.29010745882987976,
        # 0.08735490590333939],
        # 'box_name': 'roof',
        # 'objectness': 0.09425540268421173}, ...
        # ]

        if len(detected_objects) == 0:
            return PlanResult(
                success=False, 
                error_message="No objects were detected in the image.",
                info_dict=dict(objects_to_detect=text_queries)
            )

        # Covert detection results to a string
        textualized_object_list = self.textualize_detections(detected_objects, include_coordinates=self.configs["include_coordinates"])
        prompt = textualized_object_list + '\n\n' + prompt
        
        plan_raw = self.llm.chat(
            prompt=prompt, 
            meta_prompt=self.meta_prompt.format(action_space=self.action_space)
        )

        masks = self.segmentor.segment_by_bboxes(image=processed_image, bboxes=[[obj['bbox']] for obj in detected_objects])

        plan_code, filtered_masks = extract_plans_and_regions(plan_raw, masks)

        return PlanResult(
            success=True,
            plan_code=plan_code,
            masks=filtered_masks,
            plan_raw=plan_raw,
            prompt=prompt,
            info_dict=dict(configs=self.configs, detected_objects=detected_objects)
        )


class VLMSeg(Agent):
    meta_prompt = \
'''
You are in charge of controlling a robot. You will be given a list of operations you are allowed to perform, along with a task to solve. 
You need to output your plan as python code.
After writing the code, you should also tell me the objects you want to interact with in your code. To reduce ambiguity, you should try to use different but simple and common names to refer to a single object. 
The object list should be a valid json format, for example, [{"name": "marker", "aliases": ["pen", "pencil"]}, {"name": "remote", "aliases": ["remote controller", "controller"]}, ...]. "aliases" should be an empty list if there are no aliases.

Operation list:
{action_space}

Note:
- Do not redefine functions in the operation list.
- For any item referenced in your code, please use the format of `object="object_name"`.
- Your object list should be encompassed by a json code block "```json".
- Your code should be surrounded by a python code block "```python".
'''
    def __init__(self, vlm: LanguageModel, detector: Detector, segmentor: Segmentor, configs: dict = None, **kwargs):
        if not isinstance(vlm, LanguageModel):
            raise TypeError("`vlm` must be an instance of LanguageModel.")
        if not isinstance(detector, Detector):
            raise TypeError("`detector` must be an instance of Detector.")
        if not isinstance(segmentor, Segmentor):
            raise TypeError("`segmentor` must be an instance of Segmentor.")

        self.vlm = vlm
        self.detector = detector
        self.segmentor = segmentor

        # Default configs
        self.configs = {
            "img_size": 640,
            "label_mode": "1",
            "alpha": 0.75
        }
        if configs is not None:
            self.configs = self.configs.update(configs)

        super().__init__(**kwargs)

    def extract_objects_of_interest_from_vlm_response(self, plan_raw: str):
        # Extract code blocks. We assume there is only one code block in the generation
        code_blocks = re.findall(r'```python(.*?)```', plan_raw, re.DOTALL)
        json_blocks = re.findall(r'```json(.*?)```', plan_raw, re.DOTALL)
        if not code_blocks or not json_blocks:
            return None, None
        
        code_block = code_blocks[0]
        json_block = json_blocks[0]
        object_names_and_aliases = json.loads(json_block)

        # Use regular expression to find all occurrences of region[index]
        # object_names = re.findall(r'object=\"(.+)\"', code_block)

        # object_names = list(set(obj_name for obj_name in object_names))

        return code_block, object_names_and_aliases

    def plan(self, prompt: str, image: Image.Image):
        # Resize the image if necessary
        processed_image = image
        if "img_size" in self.configs:
            processed_image = resize_image(image, self.configs["img_size"])

        # Generate a textual response from VLM
        plan_raw = self.vlm.chat(
            prompt=prompt, 
            image=processed_image, 
            meta_prompt=self.meta_prompt.format(action_space=self.action_space)
        )

        # Extract objects of interest from VLM's response
        plan_code, object_names_and_aliases = self.extract_objects_of_interest_from_vlm_response(plan_raw)
        if objects_of_interest is None:
            return PlanResult(
                success=False,
                error_message=f"Could not extract objects of intereset.",
                plan_raw=plan_raw,
                info_dict=dict(configs=self.configs)
            )
        
        objects_of_interest = [obj["name"] for obj in object_names_and_aliases]

        # Detect only the objects of interest
        detected_objects = self.detector.detect_objects(
            processed_image,
            objects_of_interest,
            bbox_score_top_k=20,
            bbox_conf_threshold=0.3
        )

        # (kaixin) NOTE: This requires the object names to be unique.
        # Filter and select boxes with the correct name and highest score per name
        best_boxes = {}
        for det in detected_objects:
            box_name = det["box_name"]
            if box_name not in best_boxes or det["score"] > best_boxes[box_name]["score"]:
                best_boxes[box_name] = det

        # Check if any object of interest is missing in the detected objects
        missing_objects = set(objects_of_interest) - set(best_boxes.keys())
        if missing_objects:
            return PlanResult(
                success=False,
                error_message=f"Missing objects that were not detected or had no best box: {', '.join(missing_objects)}",
                info_dict=dict(
                    objects_to_detect=objects_of_interest, 
                    found_objects=list(best_boxes.keys()), 
                    missing_objects=list(missing_objects), 
                    info_dict=dict(configs=self.configs)
                )
            )

        # Arrange boxes in the order of objects_of_interest
        boxes_of_interest = [best_boxes[name] for name in objects_of_interest]

        
        # Draw masks
        annotated_img = visualize_bboxes(
            processed_image,
            bboxes=[obj['bbox'] for obj in boxes_of_interest], 
            alpha=self.configs["alpha"]
        )

        masks = self.segmentor.segment_by_bboxes(image=image, bboxes=[[bbox] for bbox in detected_objects])

        # Generate the final plan using the VLM with the annotated image
        # final_plan = self.vlm.chat(
        #     prompt=prompt, 
        #     image=annotated_img, 
        #     meta_prompt=self.meta_prompt.format(action_space=self.action_space)
        # )

        # plan_code, filtered_masks = extract_plans_and_regions(final_plan, masks)

        # Replace object names with region masks
        for index, object_name in enumerate(objects_of_interest):
            plan_code = plan_code.replace(object_name, f"regions[{str(index)}]")

        return PlanResult(
            success=True,
            plan_code=plan_code,
            masks=masks,
            plan_raw=plan_raw,
            annotated_image=annotated_img,
            prompt=prompt,
            info_dict=dict(configs=self.configs)
        )


def agent_factory(agent_type, segmentor=None, vlm=None, detector=None, llm=None, configs=None):
    """
    Factory method to create an instance of a specific Agent subclass with default values.

    Args:
        agent_type (str): The type of agent to create. Possible values are 'SegVLM', 'DetVLM', and 'DetLLM'.
        segmentor (Segmentor, optional): An instance of Segmentor. Defaults to a default instance if not provided.
        vlm (LanguageModel, optional): An instance of LanguageModel for VLM. Defaults to a default instance if not provided.
        detector (Detector, optional): An instance of Detector. Defaults to a default instance if not provided.
        llm (LanguageModel, optional): An instance of LanguageModel for LLM. Defaults to a default instance if not provided.
        configs (dict, optional): A dictionary of configuration settings.

    Returns:
        Agent: An instance of the specified Agent subclass.
    """

    # Use default instances if none are provided
    from api.detectors import OWLViT
    from api.segmentors import SAM
    from api.language_model import GPT4, GPT4V
    segmentor = segmentor or SAM()
    vlm = vlm or GPT4V()
    detector = detector or OWLViT()
    llm = llm or GPT4()

    if agent_type == 'SegVLM':
        return SegVLM(segmentor=segmentor, vlm=vlm, configs=configs)

    elif agent_type == 'DetVLM':
        return DetVLM(segmentor=segmentor, detector=detector, vlm=vlm, configs=configs)

    elif agent_type == 'DetLLM':
        return DetLLM(segmentor=segmentor, detector=detector, llm=llm, configs=configs)

    elif agent_type == 'VLMSeg':
        return VLMSeg(segmentor=segmentor, detector=detector, vlm=vlm, configs=configs)

    else:
        raise ValueError("Unknown agent type.")
    
