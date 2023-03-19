class SettingsGrid {
    constructor(element_id, ws_endpoint, columns = ["key", "value"]) {
        this.element_id = element_id

        this.settings_ws = new ReconnectingWebSocket(ws_endpoint);

        this.columns = columns

        var me = this
   
        this.myAppendGrid = new AppendGrid({
            element: element_id,
            uiFramework: "bootstrap4",
            iconFramework: "fontawesome5",
            initRows: 0,
            columns: function(){
                var rendered_columns = []

                columns.forEach(function(column) {
                    rendered_columns.push({
                        name: column,
                        display: column.charAt(0).toUpperCase() + column.slice(1),
                        events: {
                            change: function(e){
                                me.put_data(e)
                            }
                        }
                    })
                })
                return rendered_columns
            }(),
            hideButtons: {
                moveUp: true,
                moveDown: true,
                insert: true,
            },
            sectionClasses: {
                table: "table-sm",
                control: "form-control-sm",
                buttonGroup: "btn-group-sm"
            },
            afterRowRemoved: function(caller, rowIndex){
                me.put_data()
            }
        });
    
        this.settings_ws.onmessage = function(event){
            var data = JSON.parse(event.data);
            me.myAppendGrid.load(data)
        }
    }

    put_data(e){
        if(e){
            var check = this.myAppendGrid.getRowValue(this.myAppendGrid.getRowIndex(e.uniqueIndex))
            var has_values = this.columns.every(function(column) {
                if(!check[column])
                    return false
                return true
            })
            if(!(has_values))
                return
        }

        var data = this.myAppendGrid.getAllValue()
        this.settings_ws.send(JSON.stringify({
            "cmd": "put",
            "data": data
        }))
    }
}