// GEMV: y = A @ x where A is (ROWS x COLS) int8 and x is (COLS) int8
// Output y is ROWS int32 values (one per row of A)
// in_a_flat: 1024 elements (16x64), row-major A, 16 elements per beat
// in_x_flat: 64 elements, 16 elements per beat

module dut #(
    parameter ROWS = 16,
    parameter COLS = 64,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    // Storage: A in columnar format (64 cols x 16 rows packed in 128 bits each)
    // col_reg[j] holds A[0][j], A[1][j], ..., A[15][j] packed as [row*8+:8] per row
    reg [LANES*DATA_W-1:0] col_reg [0:COLS-1];
    
    // x values: 64 int8 values packed in order
    reg [DATA_W*COLS-1:0] x_buf;
    
    // State machine
    localparam IDLE       = 2'd0;
    localparam BUFFERING  = 2'd1;
    localparam COMPUTING = 2'd2;
    localparam OUTPUT     = 2'd3;
    
    reg [1:0] state, next_state;
    
    // Beat counters
    reg [6:0] col_cnt;
    reg [2:0] x_cnt;
    
    // Output counter
    reg [4:0] out_cnt;  // 0-16
    
    // Flags for completion
    reg a_done;
    reg x_done;
    
    // 16 parallel accumulators, one per row
    reg signed [ACC_W-1:0] acc [0:ROWS-1];
    
    // Integer for loop
    integer j;
    
    // Next state logic
    always @(*) begin
        next_state = state;
        case (state)
            IDLE: begin
                if (in_a_flat_valid || in_x_flat_valid)
                    next_state = BUFFERING;
            end
            
            BUFFERING: begin
                if (a_done && x_done)
                    next_state = COMPUTING;
            end
            
            COMPUTING: begin
                if (col_cnt == 7'd62)  // About to process last column
                    next_state = OUTPUT;
            end
            
            OUTPUT: begin
                if (out_ready && out_cnt == 5'd15)
                    next_state = IDLE;
            end
        endcase
    end
    
    // FSM register updates
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= IDLE;
            col_cnt <= 7'd0;
            x_cnt <= 3'd0;
            out_cnt <= 5'd0;
            out_valid <= 1'b0;
            a_done <= 1'b0;
            x_done <= 1'b0;
            for (j = 0; j < ROWS; j = j + 1) begin
                acc[j] <= {ACC_W{1'b0}};
            end
        end else begin
            state <= next_state;
            
            case (state)
                IDLE: begin
                    out_valid <= 1'b0;
                    out_cnt <= 5'd0;
                    a_done <= 1'b0;
                    x_done <= 1'b0;
                    col_cnt <= 7'd0;
                    x_cnt <= 3'd0;
                    for (j = 0; j < ROWS; j = j + 1) begin
                        acc[j] <= {ACC_W{1'b0}};
                    end
                    // Store first beat
                    if (in_a_flat_valid)
                        col_reg[0] <= in_a_flat;
                    if (in_x_flat_valid)
                        x_buf[LANES*DATA_W-1:0] <= in_x_flat;
                end
                
                BUFFERING: begin
                    // Handle A input (beats 0-63)
                    if (in_a_flat_valid) begin
                        if (col_cnt < 7'd63)
                            col_reg[col_cnt + 1] <= in_a_flat;
                        col_cnt <= col_cnt + 7'd1;
                        if (col_cnt == 7'd62)
                            a_done <= 1'b1;
                    end
                    
                    // Handle x input (beats 0-3)
                    if (in_x_flat_valid) begin
                        x_buf[x_cnt*LANES*DATA_W +: LANES*DATA_W] <= in_x_flat;
                        x_cnt <= x_cnt + 3'd1;
                        if (x_cnt == 3'd3)
                            x_done <= 1'b1;
                    end
                end
                
                COMPUTING: begin
                    // 16 parallel multiply-accumulate for 16 rows
                    for (j = 0; j < ROWS; j = j + 1) begin
                        acc[j] <= acc[j] + 
                                  ({{24{1'b0}}, x_buf[col_cnt*8 +: 8]}) * 
                                  ({{16{col_reg[col_cnt][j*8 + 7]}}, col_reg[col_cnt][j*8 +: 8]});
                    end
                    col_cnt <= col_cnt + 7'd1;
                end
                
                OUTPUT: begin
                    out_valid <= 1'b1;
                    if (out_ready) begin
                        out_cnt <= out_cnt + 5'd1;
                    end
                end
            endcase
        end
    end
    
    // Ready signals - accept new transaction only when idle
    assign in_a_flat_ready = (state == IDLE);
    assign in_x_flat_ready = (state == IDLE);
    
    // Output data - select based on out_cnt
    reg signed [ACC_W-1:0] out_c_r;
    always @(*) begin
        case (out_cnt[3:0])
            4'd0: out_c_r = acc[0];
            4'd1: out_c_r = acc[1];
            4'd2: out_c_r = acc[2];
            4'd3: out_c_r = acc[3];
            4'd4: out_c_r = acc[4];
            4'd5: out_c_r = acc[5];
            4'd6: out_c_r = acc[6];
            4'd7: out_c_r = acc[7];
            4'd8: out_c_r = acc[8];
            4'd9: out_c_r = acc[9];
            4'd10: out_c_r = acc[10];
            4'd11: out_c_r = acc[11];
            4'd12: out_c_r = acc[12];
            4'd13: out_c_r = acc[13];
            4'd14: out_c_r = acc[14];
            4'd15: out_c_r = acc[15];
            default: out_c_r = {ACC_W{1'b0}};
        endcase
    end
    assign out_c = out_c_r;
    
    // Output valid
    reg out_valid;
    always @(*) begin
        if (state == OUTPUT)
            out_valid = 1'b1;
        else
            out_valid = 1'b0;
    end

endmodule
