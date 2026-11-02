import pytest
from competitive_prompting.phase3.extension_v1.reporting_v1.complete import historical_match, read_csv


def test_historical_contrast_label_is_not_extension_label():
    row={'model':'m','category':'all','metric':'operational_task_success','comparison':'primary_C_minus_B'}
    assert historical_match([row],'m','operational_task_success')==row
    with pytest.raises(ValueError): historical_match([{**row,'comparison':'CB'}],'m','operational_task_success')
    with pytest.raises(ValueError): historical_match([row,row],'m','operational_task_success')


def test_csv_metric_types(tmp_path):
    p=tmp_path/'metrics.csv';p.write_text('model,n,delta,ci,flag\nm,54,-0.1,,True\n',encoding='utf-8')
    assert read_csv(p)==[{'model':'m','n':54,'delta':-.1,'ci':None,'flag':True}]
